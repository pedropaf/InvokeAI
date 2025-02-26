import { Flex } from '@chakra-ui/react';
import { useAppDispatch } from 'app/store/storeHooks';
import { InvControl } from 'common/components/InvControl/InvControl';
import { InvSelect } from 'common/components/InvSelect/InvSelect';
import { useGroupedModelInvSelect } from 'common/components/InvSelect/useGroupedModelInvSelect';
import { SyncModelsIconButton } from 'features/modelManager/components/SyncModels/SyncModelsIconButton';
import { fieldMainModelValueChanged } from 'features/nodes/store/nodesSlice';
import type {
  SDXLMainModelFieldInputInstance,
  SDXLMainModelFieldInputTemplate,
} from 'features/nodes/types/field';
import { memo, useCallback } from 'react';
import { SDXL_MAIN_MODELS } from 'services/api/constants';
import type { MainModelConfigEntity } from 'services/api/endpoints/models';
import { useGetMainModelsQuery } from 'services/api/endpoints/models';

import type { FieldComponentProps } from './types';

type Props = FieldComponentProps<
  SDXLMainModelFieldInputInstance,
  SDXLMainModelFieldInputTemplate
>;

const SDXLMainModelFieldInputComponent = (props: Props) => {
  const { nodeId, field } = props;
  const dispatch = useAppDispatch();
  const { data, isLoading } = useGetMainModelsQuery(SDXL_MAIN_MODELS);
  const _onChange = useCallback(
    (value: MainModelConfigEntity | null) => {
      if (!value) {
        return;
      }
      dispatch(
        fieldMainModelValueChanged({
          nodeId,
          fieldName: field.name,
          value,
        })
      );
    },
    [dispatch, field.name, nodeId]
  );
  const { options, value, onChange, placeholder, noOptionsMessage } =
    useGroupedModelInvSelect({
      modelEntities: data,
      onChange: _onChange,
      isLoading,
      selectedModel: field.value,
    });

  return (
    <Flex w="full" alignItems="center" gap={2}>
      <InvControl
        className="nowheel nodrag"
        isDisabled={!options.length}
        isInvalid={!value}
      >
        <InvSelect
          value={value}
          placeholder={placeholder}
          options={options}
          onChange={onChange}
          noOptionsMessage={noOptionsMessage}
        />
      </InvControl>
      <SyncModelsIconButton className="nodrag" />
    </Flex>
  );
};

export default memo(SDXLMainModelFieldInputComponent);
