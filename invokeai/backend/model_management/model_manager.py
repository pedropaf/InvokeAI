"""This module manages the InvokeAI `models.yaml` file, mapping
symbolic diffusers model names to the paths and repo_ids used
by the underlying `from_pretrained()` call.

For fetching models, use manager.get_model('symbolic name'). This will
return a SDModelInfo object that contains the following attributes:
   
   * context -- a context manager Generator that loads and locks the 
                model into GPU VRAM and returns the model for use. 
                See below for usage.
   * name -- symbolic name of the model
   * type -- SDModelType of the model
   * hash -- unique hash for the model
   * location -- path or repo_id of the model
   * revision -- revision of the model if coming from a repo id,
                e.g. 'fp16'
   * precision -- torch precision of the model

Typical usage:

   from invokeai.backend import ModelManager

   manager = ModelManager(
                 config='./configs/models.yaml',
                 max_cache_size=8
             ) # gigabytes

   model_info = manager.get_model('stable-diffusion-1.5', SDModelType.Diffusers)
   with model_info.context as my_model:
      my_model.latents_from_embeddings(...)

The manager uses the underlying ModelCache class to keep
frequently-used models in RAM and move them into GPU as needed for
generation operations. The optional `max_cache_size` argument
indicates the maximum size the cache can grow to, in gigabytes. The
underlying ModelCache object can be accessed using the manager's "cache"
attribute.

Because the model manager can return multiple different types of
models, you may wish to add additional type checking on the class
of model returned. To do this, provide the option `model_type`
parameter:

    model_info = manager.get_model(
                      'clip-tokenizer',
                       model_type=SDModelType.Tokenizer
                      )

This will raise an InvalidModelError if the format defined in the
config file doesn't match the requested model type.

MODELS.YAML

The general format of a models.yaml section is:

 type-of-model/name-of-model:
     path: /path/to/local/file/or/directory
     description: a description
     format: folder|ckpt|safetensors|pt
     base: SD-1|SD-2
     subfolder: subfolder-name

The type of model is given in the stanza key, and is one of
{diffusers, ckpt, vae, text_encoder, tokenizer, unet, scheduler,
safety_checker, feature_extractor, lora, textual_inversion,
controlnet}, and correspond to items in the SDModelType enum defined
in model_cache.py

The format indicates whether the model is organized as a folder with
model subdirectories, or is contained in a single checkpoint or
safetensors file.

One, but not both, of repo_id and path are provided. repo_id is the
HuggingFace repository ID of the model, and path points to the file or
directory on disk.

If subfolder is provided, then the model exists in a subdirectory of
the main model. These are usually named after the model type, such as
"unet".

This example summarizes the two ways of getting a non-diffuser model:

 text_encoder/clip-test-1:
   format: folder
   path: /path/to/folder
   description: Returns standalone CLIPTextModel

 text_encoder/clip-test-2:
   format: folder
   repo_id: /path/to/folder
   subfolder: text_encoder
   description: Returns the text_encoder in the subfolder of the diffusers model (just the encoder in RAM)

SUBMODELS:

It is also possible to fetch an isolated submodel from a diffusers
model. Use the `submodel` parameter to select which part:

 vae = manager.get_model('stable-diffusion-1.5',submodel=SDModelType.Vae)
 with vae.context as my_vae:
    print(type(my_vae))
    # "AutoencoderKL"

DIRECTORY_SCANNING:

Loras, textual_inversion and controlnet models are usually not listed
explicitly in models.yaml, but are added to the in-memory data
structure at initialization time by scanning the models directory. The
in-memory data structure can be resynchronized by calling
`manager.scan_models_directory`.

DISAMBIGUATION:

You may wish to use the same name for a related family of models. To
do this, disambiguate the stanza key with the model and and format
separated by "/". Example:

 tokenizer/clip-large:
   format: tokenizer
   path: /path/to/folder
   description: Returns standalone tokenizer

 text_encoder/clip-large:
   format: text_encoder
   path: /path/to/folder
   description: Returns standalone text encoder

You can now use the `model_type` argument to indicate which model you
want:

 tokenizer = mgr.get('clip-large',model_type=SDModelType.Tokenizer)
 encoder = mgr.get('clip-large',model_type=SDModelType.TextEncoder)

OTHER FUNCTIONS:

Other methods provided by ModelManager support importing, editing,
converting and deleting models.

IMPORTANT CHANGES AND LIMITATIONS SINCE 2.3:

1. Only local paths are supported. Repo_ids are no longer accepted. This
simplifies the logic.

2. VAEs can't be swapped in and out at load time. They must be baked
into the model when downloaded or converted.

"""
from __future__ import annotations

import os
import re
import textwrap
import shutil
import traceback
from dataclasses import dataclass
from enum import Enum, auto
from packaging import version
from pathlib import Path
from typing import Callable, Dict, Optional, List, Tuple, Union, types
from shutil import rmtree

import safetensors
import safetensors.torch
import torch
from diffusers import AutoencoderKL
from huggingface_hub import scan_cache_dir
from omegaconf import OmegaConf
from omegaconf.dictconfig import DictConfig

from pydantic import BaseModel

import invokeai.backend.util.logging as logger
from invokeai.app.services.config import InvokeAIAppConfig
from invokeai.backend.util import CUDA_DEVICE, download_with_resume
from ..install.model_install_backend import Dataset_path, hf_download_with_resume
from .model_cache import ModelCache, ModelLocker, SilenceWarnings
from .models import BaseModelType, ModelType, SubModelType, MODEL_CLASSES
# We are only starting to number the config file with release 3.
# The config file version doesn't have to start at release version, but it will help
# reduce confusion.
CONFIG_FILE_VERSION='3.0.0'

# wanted to use pydantic here, but Generator objects not supported
@dataclass
class SDModelInfo():
    context: ModelLocker
    name: str
    type: SDModelType
    hash: str
    location: Union[Path,str]
    precision: torch.dtype
    revision: str = None
    _cache: ModelCache = None

    def __enter__(self):
        return self.context.__enter__()

    def __exit__(self,*args, **kwargs):
        self.context.__exit__(*args, **kwargs)

class InvalidModelError(Exception):
    "Raised when an invalid model is requested"
    pass

MAX_CACHE_SIZE = 6.0  # GB


# layout of the models directory:
# models
# ├── SD-1
# │   ├── controlnet
# │   ├── lora
# │   ├── diffusers
# │   └── textual_inversion
# ├── SD-2
# │   ├── controlnet
# │   ├── lora
# │   ├── diffusers
# │   └── textual_inversion
# └── support
#     ├── codeformer
#     ├── gfpgan
#     └── realesrgan


class ConfigMeta(BaseModel):
    version: str

class ModelManager(object):
    """
    High-level interface to model management.
    """

    logger: types.ModuleType = logger

    def __init__(
        self,
        config: Union[Path, DictConfig, str],
        device_type: torch.device = CUDA_DEVICE,
        precision: torch.dtype = torch.float16,
        max_cache_size=MAX_CACHE_SIZE,
        sequential_offload=False,
        logger: types.ModuleType = logger,
    ):
        """
        Initialize with the path to the models.yaml config file. 
        Optional parameters are the torch device type, precision, max_models,
        and sequential_offload boolean. Note that the default device
        type and precision are set up for a CUDA system running at half precision.
        """

        self.config_path = None
        if isinstance(config, (str, Path)):
            self.config_path = Path(config)
            config = OmegaConf.load(self.config_path)

        elif not isinstance(config, DictConfig):
            raise ValueError('config argument must be an OmegaConf object, a Path or a string')

        config_meta = ConfigMeta(config.pop("__metadata__")) # TODO: naming
        # TODO: metadata not found

        self.models = dict()
        for model_key, model_config in config.items():
            model_name, base_model, model_type = self.parse_key(model_key)
            model_class = MODEL_CLASSES[base_model][model_type]
            self.models[model_key] = model_class.build_config(**model_config)

        # check config version number and update on disk/RAM if necessary
        self.globals = InvokeAIAppConfig.get_config()
        self._update_config_file_version()
        self.logger = logger
        self.cache = ModelCache(
            max_cache_size=max_cache_size,
            execution_device = device_type,
            precision = precision,
            sequential_offload = sequential_offload,
            logger = logger,
        )
        self.cache_keys = dict()

        # add controlnet, lora and textual_inversion models from disk
        self.scan_models_directory(include_diffusers=False)

    def model_exists(
        self,
        model_name: str,
        base_model: BaseModelType,
        model_type: ModelType,
    ) -> bool:
        """
        Given a model name, returns True if it is a valid
        identifier.
        """
        model_key = self.create_key(model_name, base_model, model_type)
        return model_key in self.models

    def create_key(
        self,
        model_name: str,
        base_model: BaseModelType,
        model_type: ModelType,
    ) -> str:
        return f"{base_model}/{model_type}/{model_name}"

    def parse_key(self, model_key: str) -> Tuple[str, BaseModelType, ModelType]:
        base_model_str, model_type_str, model_name = model_key.split('/', 2)
        try:
            model_type = SDModelType(model_type_str)
        except:
            raise Exception(f"Unknown model type: {model_type_str}")

        try:
            base_model = BaseModelType(base_model_str)
        except:
            raise Exception(f"Unknown base model: {base_model_str}")

        return (model_name, base_model, model_type)

    def get_model(
        self,
        model_name: str,
        base_model: BaseModelType,
        model_type: ModelType,
        submodel_type: Optional[SubModelType] = None
    ):
        """Given a model named identified in models.yaml, return
        an SDModelInfo object describing it.
        :param model_name: symbolic name of the model in models.yaml
        :param model_type: SDModelType enum indicating the type of model to return
        :param submodel: an SDModelType enum indicating the portion of 
               the model to retrieve (e.g. SDModelType.Vae)

        If not provided, the model_type will be read from the `format` field
        of the corresponding stanza. If provided, the model_type will be used
        to disambiguate stanzas in the configuration file. The default is to
        assume a diffusers pipeline. The behavior is illustrated here:

        [models.yaml]
        diffusers/test1:
           repo_id: foo/bar
           description: Typical diffusers pipeline

        lora/test1:
           repo_id: /tmp/loras/test1.safetensors
           description: Typical lora file

        test1_pipeline = mgr.get_model('test1')
        # returns a StableDiffusionGeneratorPipeline

        test1_vae1 = mgr.get_model('test1', submodel=SDModelType.Vae)
        # returns the VAE part of a diffusers model as an AutoencoderKL

        test1_vae2 = mgr.get_model('test1', model_type=SDModelType.Diffusers, submodel=SDModelType.Vae)
        # does the same thing as the previous  statement. Note that model_type
        # is for the parent model, and submodel is for the part

        test1_lora = mgr.get_model('test1', model_type=SDModelType.Lora)
        # returns a LoRA embed (as a 'dict' of tensors)

        test1_encoder = mgr.get_modelI('test1', model_type=SDModelType.TextEncoder)
        # raises an InvalidModelError

        """

        model_class = MODEL_CLASSES[base_model][model_type]

        model_key = self.create_key(model_name, base_model, model_type)

        # if model not found try to find it (maybe file just pasted)
        if model_key not in self.models:
            # TODO: find by mask or try rescan?
            path_mask = f"/models/{base_model}/{model_type}/{model_name}*"
            if False: # model_path = next(find_by_mask(path_mask)):
                model_path = None # TODO:
                model_config = model_class.build_config(
                    path=model_path,
                )
                self.models[model_key] = model_config
            else:
                raise Exception(f"Model not found - {model_key}")

        # if it known model check that target path exists (if manualy deleted)
        else:
            # logic repeated twice(in rescan too) any way to optimize?
            if not os.path.exists(self.models[model_key].path):
                if model_class.save_to_config:
                    self.models[model_key].error = ModelError.NotFound
                    raise Exception(f"Files for model \"{model_key}\" not found")

                else:
                    self.models.pop(model_key, None)
                    raise Exception(f"Model not found - {model_key}")

            # reset model errors?



        model_config = self.models[model_key]
            
        # /models/{base_model}/{model_type}/{name}.ckpt or .safentesors
        # /models/{base_model}/{model_type}/{name}/
        model_path = model_config.path

        # vae/movq override
        # TODO: 
        if submodel is not None and submodel in model_config:
            model_path = model_config[submodel]
            model_type = submodel
            submodel = None

        dst_convert_path = None # TODO:
        model_path = model_class.convert_if_required(
            model_path,
            dst_convert_path,
            model_config,
        )

        model_context = self.cache.get_model(
            model_path,
            model_class,
            submodel,
        )

        hash = "<NO_HASH>" # TODO:
            
        return SDModelInfo(
            context = model_context,
            name = model_name,
            base_model = base_model,
            type = submodel or model_type,
            hash = hash,
            location = model_path, # TODO:
            precision = self.cache.precision,
            _cache = self.cache,
        )

    def default_model(self) -> Optional[Tuple[str, BaseModelType, ModelType]]:
        """
        Returns the name of the default model, or None
        if none is defined.
        """
        for model_key, model_config in self.models.items():
            if model_config.default:
                return self.parse_key(model_key)

        for model_key, _ in self.models.items():
            return self.parse_key(model_key)
        else:
            return None # TODO: or redo as (None, None, None)

    def set_default_model(
        self,
        model_name: str,
        base_model: BaseModelType,
        model_type: ModelType,
    ) -> None:
        """
        Set the default model. The change will not take
        effect until you call model_manager.commit()
        """

        model_key = self.model_key(model_name, base_model, model_type)
        if model_key not in self.models:
            raise Exception(f"Unknown model: {model_key}")

        for cur_model_key, config in self.models.items():
            config.default = cur_model_key == model_key

    def model_info(
        self,
        model_name: str,
        base_model: BaseModelType,
        model_type: ModelType,
    ) -> dict:
        """
        Given a model name returns the OmegaConf (dict-like) object describing it.
        """
        model_key = self.create_key(model_name, base_model, model_type)
        if model_key in self.models:
            return self.models[model_key].dict(exclude_defaults=True)
        else:
            return None # TODO: None or empty dict on not found

    def model_names(self) -> List[Tuple[str, BaseModelType, ModelType]]:
        """
        Return a list of (str, BaseModelType, ModelType) corresponding to all models 
        known to the configuration.
        """
        return [(self.parse_key(x)) for x in self.models.keys()]

    def list_models(
        self,
        base_model: Optional[BaseModelType] = None,
        model_type: Optional[SDModelType] = None,
    ) -> Dict[str, Dict[str, str]]:
        """
        Return a dict of models, in format [base_model][model_type][model_name]

        Please use model_manager.models() to get all the model names,
        model_manager.model_info('model-name') to get the stanza for the model
        named 'model-name', and model_manager.config to get the full OmegaConf
        object derived from models.yaml
        """
        assert not(model_type is not None and base_model is None), "model_type must be provided with base_model"

        models = dict()
        for model_key in sorted(self.models, key=str.casefold):
            model_config = self.models[model_key]

            cur_model_name, cur_base_model, cur_model_type = self.parse_key(model_key)
            if base_model is not None and cur_base_model != base_model:
                continue
            if model_type is not None and cur_model_type != model_type:
                continue

            if cur_base_model not in models:
                models[cur_base_model] = dict()
            if cur_model_type not in models[cur_base_model]:
                models[cur_base_model][cur_model_type] = dict()

            models[m_base_model][stanza_type][model_name] = dict(
                **model_config.dict(exclude_defaults=True),
                name=model_name,
                base_model=cur_base_model,
                type=cur_model_type,
            )

        return models

    def print_models(self) -> None:
        """
        Print a table of models, their descriptions
        """
        # TODO: redo
        for model_type, model_dict in self.list_models().items():
            for model_name, model_info in model_dict.items():
                line = f'{model_info["name"]:25s} {model_info["type"]:10s} {model_info["description"]}'
                print(line)

    def del_model(
        self,
        model_name: str,
        base_model: BaseModelType,
        model_type: ModelType,
        delete_files: bool = False,
    ):
        """
        Delete the named model.
        """
        raise Exception("TODO: del_model") # TODO: redo
        model_key = self.create_key(model_name, base_model, model_type)
        model_cfg = self.models.pop(model_key, None)

        if model_cfg is None:
            self.logger.error(
                f"Unknown model {model_key}"
            )
            return

        # TODO: some legacy?
        #if model_name in self.stack:
        #    self.stack.remove(model_name)

        if delete_files:
            repo_id = model_cfg.get("repo_id", None)
            path    = self._abs_path(model_cfg.get("path", None))
            weights = self._abs_path(model_cfg.get("weights", None))
            if "weights" in model_cfg:
                weights = self._abs_path(model_cfg["weights"])
                self.logger.info(f"Deleting file {weights}")
                Path(weights).unlink(missing_ok=True)

            elif "path" in model_cfg:
                path = self._abs_path(model_cfg["path"])
                self.logger.info(f"Deleting directory {path}")
                rmtree(path, ignore_errors=True)

            elif "repo_id" in model_cfg:
                repo_id = model_cfg["repo_id"]
                self.logger.info(f"Deleting the cached model directory for {repo_id}")
                self._delete_model_from_cache(repo_id)

    def add_model(
        self,
        model_name: str,
        base_model: BaseModelType,
        model_type: ModelType,
        model_attributes: dict,
        clobber: bool = False,
    ) -> None:
        """
        Update the named model with a dictionary of attributes. Will fail with an
        assertion error if the name already exists. Pass clobber=True to overwrite.
        On a successful update, the config will be changed in memory and the
        method will return True. Will fail with an assertion error if provided
        attributes are incorrect or the model name is missing.
        """

        model_class = MODEL_CLASSES[base_model][model_type]
        model_config = model_class.build_config(**model_attributes)
        model_key = self.create_key(model_name, base_model, model_type)

        assert (
            clobber or model_key not in self.models
        ), f'attempt to overwrite existing model definition "{model_key}"'

        self.models[model_key] = model_config
            
        if clobber and model_key in self.cache_keys:
            # TODO:
            self.cache.uncache_model(self.cache_keys[model_key])
            del self.cache_keys[model_key]

    def import_diffuser_model(
        self,
        repo_or_path: Union[str, Path],
        model_name: str = None,
        description: str = None,
        vae: dict = None,
        commit_to_conf: Path = None,
    ) -> bool:
        """
        Attempts to install the indicated diffuser model and returns True if successful.

        "repo_or_path" can be either a repo-id or a path-like object corresponding to the
        top of a downloaded diffusers directory.

        You can optionally provide a model name and/or description. If not provided,
        then these will be derived from the repo name. If you provide a commit_to_conf
        path to the configuration file, then the new entry will be committed to the
        models.yaml file.
        """
        model_name = model_name or Path(repo_or_path).stem
        model_description = description or f"Imported diffusers model {model_name}"
        new_config = dict(
            description=model_description,
            vae=vae,
            format="diffusers",
        )
        if isinstance(repo_or_path, Path) and repo_or_path.exists():
            new_config.update(path=str(repo_or_path))
        else:
            new_config.update(repo_id=repo_or_path)

        self.add_model(model_name, SDModelType.Diffusers, new_config, True)
        if commit_to_conf:
            self.commit(commit_to_conf)
        return self.create_key(model_name, SDModelType.Diffusers)

    def import_lora(
        self,
        path: Path,
        model_name: Optional[str] = None,
        description: Optional[str] = None,
    ):
        """
        Creates an entry for the indicated lora file. Call
        mgr.commit() to write out the configuration to models.yaml
        """
        path = Path(path)
        model_name = model_name or path.stem
        model_description = description or f"LoRA model {model_name}"
        self.add_model(
            model_name,
            SDModelType.Lora,
            dict(
                format="lora",
                weights=str(path),
                description=model_description,
            ),
            True
        )
        
    def import_embedding(
        self,
        path: Path,
        model_name: Optional[str] = None,
        description: Optional[str] = None,
    ):
        """
        Creates an entry for the indicated lora file. Call
        mgr.commit() to write out the configuration to models.yaml
        """
        path = Path(path)
        if path.is_directory() and (path / "learned_embeds.bin").exists():
            weights = path / "learned_embeds.bin"
        else:
            weights = path
            
        model_name = model_name or path.stem
        model_description = description or f"Textual embedding model {model_name}"
        self.add_model(
            model_name,
            SDModelType.TextualInversion,
            dict(
                format="textual_inversion",
                weights=str(weights),
                description=model_description,
            ),
            True
        )
            
    def convert_and_import(
        self,
        ckpt_path: Path,
        diffusers_path: Path,
        model_name=None,
        model_description=None,
        vae: dict = None,
        vae_path: Path = None,
        original_config_file: Path = None,
        commit_to_conf: Path = None,
        scan_needed: bool = True,
    ) -> str:
        """
        Convert a legacy ckpt weights file to diffuser model and import
        into models.yaml.
        """
        ckpt_path = self._resolve_path(ckpt_path, "models/ldm/stable-diffusion-v1")
        if original_config_file:
            original_config_file = self._resolve_path(
                original_config_file, "configs/stable-diffusion"
            )

        new_config = None

        if diffusers_path.exists():
            self.logger.error(
                f"The path {str(diffusers_path)} already exists. Please move or remove it and try again."
            )
            return

        model_name = model_name or diffusers_path.name
        model_description = model_description or f"Converted version of {model_name}"
        self.logger.debug(f"Converting {model_name} to diffusers (30-60s)")

        # to avoid circular import errors
        from .convert_ckpt_to_diffusers import convert_ckpt_to_diffusers

        try:
            # By passing the specified VAE to the conversion function, the autoencoder
            # will be built into the model rather than tacked on afterward via the config file
            vae_model = None
            if vae:
                vae_location = self.globals.root_dir / vae.get('path') \
                    if vae.get('path') \
                       else vae.get('repo_id')
                vae_model = self.cache.get_model(vae_location, SDModelType.Vae).model
                vae_path = None
            convert_ckpt_to_diffusers(
                ckpt_path,
                diffusers_path,
                extract_ema=True,
                original_config_file=original_config_file,
                vae=vae_model,
                vae_path=vae_path,
                scan_needed=scan_needed,
            )
            self.logger.debug(
                f"Success. Converted model is now located at {str(diffusers_path)}"
            )
            self.logger.debug(f"Writing new config file entry for {model_name}")
            new_config = dict(
                path=str(diffusers_path),
                description=model_description,
                format="diffusers",
            )
            if self.model_exists(model_name, SDModelType.Diffusers):
                self.del_model(model_name, SDModelType.Diffusers)
            self.add_model(
                model_name,
                SDModelType.Diffusers,
                new_config,
                True
            )
            if commit_to_conf:
                self.commit(commit_to_conf)
            self.logger.debug(f"Model {model_name} installed")
        except Exception as e:
            self.logger.warning(f"Conversion failed: {str(e)}")
            self.logger.warning(traceback.format_exc())
            self.logger.warning(
                "If you are trying to convert an inpainting or 2.X model, please indicate the correct config file (e.g. v1-inpainting-inference.yaml)"
            )

        return model_name

    def search_models(self, search_folder):
        self.logger.info(f"Finding Models In: {search_folder}")
        models_folder_ckpt = Path(search_folder).glob("**/*.ckpt")
        models_folder_safetensors = Path(search_folder).glob("**/*.safetensors")

        ckpt_files = [x for x in models_folder_ckpt if x.is_file()]
        safetensor_files = [x for x in models_folder_safetensors if x.is_file()]

        files = ckpt_files + safetensor_files

        found_models = []
        for file in files:
            location = str(file.resolve()).replace("\\", "/")
            if (
                "model.safetensors" not in location
                and "diffusion_pytorch_model.safetensors" not in location
            ):
                found_models.append({"name": file.stem, "location": location})

        return search_folder, found_models

    def commit(self, conf_file: Path=None) -> None:
        """
        Write current configuration out to the indicated file.
        """
        data_to_save = dict()
        for model_key, model_config in self.models.items():
            model_name, base_model, model_type = self.parse_key(model_key)
            model_class = MODEL_CLASSES[base_model][model_type]
            if model_class.save_to_config:
                # TODO: or exclude_unset better fits here?
                data_to_save[model_key] = model_config.dict(exclude_defaults=True)

        yaml_str = OmegaConf.to_yaml(data_to_save)
        config_file_path = conf_file or self.config_path
        assert config_file_path is not None,'no config file path to write to'
        config_file_path = self.globals.root_dir / config_file_path
        tmpfile = os.path.join(os.path.dirname(config_file_path), "new_config.tmp")
        with open(tmpfile, "w", encoding="utf-8") as outfile:
            outfile.write(self.preamble())
            outfile.write(yaml_str)
        os.replace(tmpfile, config_file_path)

    def preamble(self) -> str:
        """
        Returns the preamble for the config file.
        """
        return textwrap.dedent(
            """\
            # This file describes the alternative machine learning models
            # available to InvokeAI script.
            #
            # To add a new model, follow the examples below. Each
            # model requires a model config file, a weights file,
            # and the width and height of the images it
            # was trained on.
        """
        )

    @classmethod
    def _delete_model_from_cache(cls,repo_id):
        cache_info = scan_cache_dir(InvokeAIAppConfig.get_config().cache_dir)

        # I'm sure there is a way to do this with comprehensions
        # but the code quickly became incomprehensible!
        hashes_to_delete = set()
        for repo in cache_info.repos:
            if repo.repo_id == repo_id:
                for revision in repo.revisions:
                    hashes_to_delete.add(revision.commit_hash)
        strategy = cache_info.delete_revisions(*hashes_to_delete)
        cls.logger.warning(
            f"Deletion of this model is expected to free {strategy.expected_freed_size_str}"
        )
        strategy.execute()

    @staticmethod
    def _abs_path(path: str | Path) -> Path:
        globals = InvokeAIAppConfig.get_config()
        if path is None or Path(path).is_absolute():
            return path
        return Path(globals.root_dir, path).resolve()

    # This is not the same as global_resolve_path(), which prepends
    # Globals.root.
    def _resolve_path(
        self, source: Union[str, Path], dest_directory: str
    ) -> Optional[Path]:
        resolved_path = None
        if str(source).startswith(("http:", "https:", "ftp:")):
            dest_directory = self.globals.root_dir / dest_directory
            dest_directory.mkdir(parents=True, exist_ok=True)
            resolved_path = download_with_resume(str(source), dest_directory)
        else:
            resolved_path = self.globals.root_dir / source
        return resolved_path

    def _update_config_file_version(self):
        # TODO: 
        raise Exception("TODO: ")

    def scan_models_directory(self):

        for model_key in list(self.models.keys()):
            model_name, base_model, model_type = self.parse_key(model_key)
            if not os.path.exists(model_config.path):
                if model_class.save_to_config:
                    self.models[model_key].error = ModelError.NotFound
                else:
                    self.models.pop(model_key, None)


        for base_model in BaseModelType:
            for model_type in ModelType:

                model_class = MODEL_CLASSES[base_model][model_type]
                models_dir = os.path.join(self.globals.models_path, base_model, model_type)
                
                for entry_name in os.listdir(models_dir):
                    model_path = os.path.join(models_dir, entry_name)
                    model_name = Path(model_path).stem
                    model_config: ModelConfigBase = model_class.build_config(
                        path=model_path,
                    )

                    model_key = self.create_key(model_name, base_model, model_type)
                    if model_key not in self.models:
                        self.models[model_key] = model_config
        
                
        
    ##### NONE OF THE METHODS BELOW WORK NOW BECAUSE OF MODEL DIRECTORY REORGANIZATION
    ##### AND NEED TO BE REWRITTEN    
    def install_lora_models(self, model_names: list[str], access_token:str=None):
        '''Download list of LoRA/LyCORIS models'''
        
        short_names = OmegaConf.load(Dataset_path).get('lora') or {}
        for name in model_names:
            name = short_names.get(name) or name

            # HuggingFace style LoRA
            if re.match(r"^[\w.+-]+/([\w.+-]+)$", name):
                self.logger.info(f'Downloading LoRA/LyCORIS model {name}')
                _,dest_dir = name.split("/")
                
                hf_download_with_resume(
                    repo_id = name,
                    model_dir = self.globals.lora_path / dest_dir,
                    model_name = 'pytorch_lora_weights.bin',
                    access_token = access_token,
                )

            elif name.startswith(("http:", "https:", "ftp:")):
                download_with_resume(name, self.globals.lora_path)

            else:
                self.logger.error(f"Unknown repo_id or URL: {name}")

    def install_ti_models(self, model_names: list[str], access_token: str=None):
        '''Download list of textual inversion embeddings'''

        short_names = OmegaConf.load(Dataset_path).get('textual_inversion') or {}
        for name in model_names:
            name = short_names.get(name) or name
            
            if re.match(r"^[\w.+-]+/([\w.+-]+)$", name):
                self.logger.info(f'Downloading Textual Inversion embedding {name}')
                _,dest_dir = name.split("/")
                hf_download_with_resume(
                    repo_id = name,
                    model_dir = self.globals.embedding_path / dest_dir,
                    model_name = 'learned_embeds.bin',
                    access_token = access_token
                )
            elif name.startswith(('http:','https:','ftp:')):
                download_with_resume(name, self.globals.embedding_path)
            else:
                self.logger.error(f'{name} does not look like either a HuggingFace repo_id or a downloadable URL')


    def install_controlnet_models(self, model_names: list[str], access_token: str=None):
        '''Download list of controlnet models; provide either repo_id or short name listed in INITIAL_MODELS.yaml'''
        short_names = OmegaConf.load(Dataset_path).get('controlnet') or {}
        dest_dir = self.globals.controlnet_path
        dest_dir.mkdir(parents=True,exist_ok=True)
        
        # The model file may be fp32 or fp16, and may be either a
        # .bin file or a .safetensors. We try each until we get one,
        # preferring 'fp16' if using half precision, and preferring
        # safetensors over over bin.
        precisions = ['.fp16',''] if self.precision=='float16' else ['']
        formats = ['.safetensors','.bin']
        possible_filenames = list()
        for p in precisions:
            for f in formats:
                possible_filenames.append(Path(f'diffusion_pytorch_model{p}{f}'))
        
        for directory_name in model_names:
            repo_id = short_names.get(directory_name) or directory_name
            safe_name = directory_name.replace('/','--')
            self.logger.info(f'Downloading ControlNet model {directory_name} ({repo_id})')
            hf_download_with_resume(
                repo_id = repo_id,
                model_dir = dest_dir / safe_name,
                model_name = 'config.json',
                access_token = access_token
            )

            path = None
            for filename in possible_filenames:
                suffix = filename.suffix
                dest_filename = Path(f'diffusion_pytorch_model{suffix}')
                self.logger.info(f'Checking availability of {directory_name}/{filename}...')
                path = hf_download_with_resume(
                    repo_id = repo_id,
                    model_dir = dest_dir / safe_name,
                    model_name = str(filename),
                    access_token = access_token,
                    model_dest = Path(dest_dir, safe_name, dest_filename),
                )
                if path:
                    (path.parent / '.download_complete').touch()
                    break


