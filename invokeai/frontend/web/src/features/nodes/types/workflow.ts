import { SyncableMap } from 'common/util/SyncableMap/SyncableMap';
import { getPrefixedId } from 'features/controlLayers/konva/util';
import { useMemo, useSyncExternalStore } from 'react';
import { z } from 'zod';

import { zFieldIdentifier } from './field';
import { zInvocationNodeData, zNotesNodeData } from './invocation';

// #region Workflow misc
const zXYPosition = z
  .object({
    x: z.number(),
    y: z.number(),
  })
  .default({ x: 0, y: 0 });
export type XYPosition = z.infer<typeof zXYPosition>;

const zWorkflowCategory = z.enum(['user', 'default', 'project']);
export type WorkflowCategory = z.infer<typeof zWorkflowCategory>;
// #endregion

// #region Workflow Nodes
const zWorkflowInvocationNode = z.object({
  id: z.string().trim().min(1),
  type: z.literal('invocation'),
  data: zInvocationNodeData,
  position: zXYPosition,
});
const zWorkflowNotesNode = z.object({
  id: z.string().trim().min(1),
  type: z.literal('notes'),
  data: zNotesNodeData,
  position: zXYPosition,
});
const zWorkflowNode = z.union([zWorkflowInvocationNode, zWorkflowNotesNode]);

type WorkflowInvocationNode = z.infer<typeof zWorkflowInvocationNode>;

export const isWorkflowInvocationNode = (val: unknown): val is WorkflowInvocationNode =>
  zWorkflowInvocationNode.safeParse(val).success;
// #endregion

// #region Workflow Edges
const zWorkflowEdgeBase = z.object({
  id: z.string().trim().min(1),
  source: z.string().trim().min(1),
  target: z.string().trim().min(1),
});
const zWorkflowEdgeDefault = zWorkflowEdgeBase.extend({
  type: z.literal('default'),
  sourceHandle: z.string().trim().min(1),
  targetHandle: z.string().trim().min(1),
  hidden: z.boolean().optional(),
});
const zWorkflowEdgeCollapsed = zWorkflowEdgeBase.extend({
  type: z.literal('collapsed'),
});
const zWorkflowEdge = z.union([zWorkflowEdgeDefault, zWorkflowEdgeCollapsed]);
// #endregion

// #region Workflow
export const zWorkflowV3 = z.object({
  id: z.string().min(1).optional(),
  name: z.string(),
  author: z.string(),
  description: z.string(),
  version: z.string(),
  contact: z.string(),
  tags: z.string(),
  notes: z.string(),
  nodes: z.array(zWorkflowNode),
  edges: z.array(zWorkflowEdge),
  exposedFields: z.array(zFieldIdentifier),
  meta: z.object({
    category: zWorkflowCategory.default('user'),
    version: z.literal('3.0.0'),
  }),
  form: z.object({
    elements: z.record(z.lazy(() => zFormElement)),
    structure: z.lazy(() => zContainerElement),
  }),
});
export type WorkflowV3 = z.infer<typeof zWorkflowV3>;
// #endregion

// #region Workflow Builder

export const elements = new SyncableMap<string, FormElement>();

export const addElement = (element: FormElement) => {
  elements.set(element.id, element);
};

export const removeElement = (id: ElementId) => {
  return elements.delete(id);
};

export const getElement = (id: ElementId) => {
  return elements.get(id);
};

export const useElement = <T extends FormElement>(id: string) => {
  const map = useSyncExternalStore(elements.subscribe, elements.getSnapshot);
  const element = useMemo(() => map.get(id), [id, map]);
  return element as T | undefined;
};

const zElementId = z.string().trim().min(1);
type ElementId = z.infer<typeof zElementId>;

const zElementBase = z.object({
  id: zElementId,
});

const NODE_FIELD_TYPE = 'node-field';
const zNodeFieldElement = zElementBase.extend({
  type: z.literal(NODE_FIELD_TYPE),
  data: z.object({
    fieldIdentifier: zFieldIdentifier,
  }),
});
export type NodeFieldElement = z.infer<typeof zNodeFieldElement>;
const nodeField = (
  nodeId: NodeFieldElement['data']['fieldIdentifier']['nodeId'],
  fieldName: NodeFieldElement['data']['fieldIdentifier']['fieldName']
): NodeFieldElement => {
  const element: NodeFieldElement = {
    id: getPrefixedId(NODE_FIELD_TYPE),
    type: NODE_FIELD_TYPE,
    data: {
      fieldIdentifier: { nodeId, fieldName },
    },
  };
  addElement(element);
  return element;
};

const HEADING_TYPE = 'heading';
const zHeadingElement = zElementBase.extend({
  type: z.literal(HEADING_TYPE),
  data: z.object({
    content: z.string(),
    level: z.union([z.literal(1), z.literal(2), z.literal(3), z.literal(4), z.literal(5)]),
  }),
});
export type HeadingElement = z.infer<typeof zHeadingElement>;
const heading = (
  content: HeadingElement['data']['content'],
  level: HeadingElement['data']['level']
): HeadingElement => {
  const element: HeadingElement = {
    id: getPrefixedId(HEADING_TYPE),
    type: HEADING_TYPE,
    data: {
      content,
      level,
    },
  };
  addElement(element);
  return element;
};

const TEXT_TYPE = 'text';
const zTextElement = zElementBase.extend({
  type: z.literal(TEXT_TYPE),
  data: z.object({
    content: z.string(),
    fontSize: z.enum(['sm', 'md', 'lg']),
  }),
});
export type TextElement = z.infer<typeof zTextElement>;
const text = (content: TextElement['data']['content'], fontSize: TextElement['data']['fontSize']): TextElement => {
  const element: TextElement = {
    id: getPrefixedId(TEXT_TYPE),
    type: TEXT_TYPE,
    data: {
      content,
      fontSize,
    },
  };
  addElement(element);
  return element;
};

const DIVIDER_TYPE = 'divider';
const zDividerElement = zElementBase.extend({
  type: z.literal(DIVIDER_TYPE),
});
export type DividerElement = z.infer<typeof zDividerElement>;
const divider = (): DividerElement => {
  const element: DividerElement = {
    id: getPrefixedId(DIVIDER_TYPE),
    type: DIVIDER_TYPE,
  };
  addElement(element);
  return element;
};

export type ContainerElement = {
  id: string;
  type: typeof CONTAINER_TYPE;
  data: {
    direction: 'row' | 'column';
    children: ElementId[];
  };
};

const CONTAINER_TYPE = 'container';
const zContainerElement: z.ZodType<ContainerElement> = zElementBase.extend({
  type: z.literal(CONTAINER_TYPE),
  data: z.object({
    direction: z.enum(['row', 'column']),
    children: z.array(zElementId),
  }),
});
const container = (
  direction: ContainerElement['data']['direction'],
  children: ContainerElement['data']['children']
): ContainerElement => {
  const element: ContainerElement = {
    id: getPrefixedId(CONTAINER_TYPE),
    type: CONTAINER_TYPE,
    data: {
      direction,
      children,
    },
  };
  addElement(element);
  return element;
};

const zFormElement = z.union([zContainerElement, zNodeFieldElement, zHeadingElement, zTextElement, zDividerElement]);

export type FormElement = z.infer<typeof zFormElement>;

export const rootId: string = container('column', [
  heading('My Cool Workflow', 1).id,
  text('This is a description of what my workflow does. It does things.', 'md').id,
  divider().id,
  heading('First Section', 2).id,
  text('The first section includes fields relevant to the first section. This note describes that fact.', 'sm').id,
  container('row', [
    nodeField('7aed1a5f-7fd7-4184-abe8-ddea0ea5e706', 'image').id,
    divider().id,
    nodeField('7aed1a5f-7fd7-4184-abe8-ddea0ea5e706', 'image').id,
    divider().id,
    nodeField('7aed1a5f-7fd7-4184-abe8-ddea0ea5e706', 'image').id,
  ]).id,
  nodeField('9c058600-8d73-4702-912b-0ccf37403bfd', 'value').id,
  nodeField('7a8bbab2-6919-4cfc-bd7c-bcfda3c79ecf', 'value').id,
  nodeField('4e16cbf6-457c-46fb-9ab7-9cb262fa1e03', 'value').id,
  nodeField('39cb5272-a9d7-4da9-9c35-32e02b46bb34', 'color').id,
  container('row', [
    container('column', [
      nodeField('4f609a81-0e25-47d1-ba0d-f24fedd5273f', 'value').id,
      nodeField('4f609a81-0e25-47d1-ba0d-f24fedd5273f', 'value').id,
    ]).id,
    container('column', [
      nodeField('4f609a81-0e25-47d1-ba0d-f24fedd5273f', 'value').id,
      nodeField('4f609a81-0e25-47d1-ba0d-f24fedd5273f', 'value').id,
      nodeField('4f609a81-0e25-47d1-ba0d-f24fedd5273f', 'value').id,
      nodeField('4f609a81-0e25-47d1-ba0d-f24fedd5273f', 'value').id,
    ]).id,
    container('column', [
      container('row', [
        nodeField('4f609a81-0e25-47d1-ba0d-f24fedd5273f', 'value').id,
        nodeField('4f609a81-0e25-47d1-ba0d-f24fedd5273f', 'value').id,
      ]).id,
      container('row', [
        nodeField('4f609a81-0e25-47d1-ba0d-f24fedd5273f', 'value').id,
        nodeField('4f609a81-0e25-47d1-ba0d-f24fedd5273f', 'value').id,
      ]).id,
    ]).id,
  ]).id,
  nodeField('14744f68-9000-4694-b4d6-cbe83ee231ee', 'model').id,
  divider().id,
  text('These are some text that are definitely super helpful.', 'sm').id,
  divider().id,
  container('row', [
    container('column', [
      nodeField('7aed1a5f-7fd7-4184-abe8-ddea0ea5e706', 'image').id,
      nodeField('7aed1a5f-7fd7-4184-abe8-ddea0ea5e706', 'image').id,
    ]).id,
    container('column', [nodeField('7a8bbab2-6919-4cfc-bd7c-bcfda3c79ecf', 'value').id]).id,
  ]).id,
]).id;
