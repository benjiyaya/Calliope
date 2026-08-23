/**
 * Shared types and helpers for the Agent composer (@workflow mentions + attachments).
 */

export type WorkflowKind = 'image' | 'video';
export type AttachmentKind = 'image' | 'video' | 'audio';

export interface WorkflowMention {
	type: 'workflow';
	id: number;
	name: string;
	kind: WorkflowKind;
}

export interface AgentAttachment {
	path: string;
	name: string;
	kind: AttachmentKind;
}

export interface AgentComposerPayload {
	content: string;
	mentions: WorkflowMention[];
	attachments: AgentAttachment[];
}

export interface WorkflowOption {
	id: number;
	name: string;
	kind: WorkflowKind;
	description?: string | null;
	is_enabled?: boolean;
}

export const MAX_WORKFLOW_MENTIONS = 1;
/** Max rows in the `@` typeahead (not the one-workflow-per-message guard). */
export const MENTION_LIMIT = 12;

/** Case-insensitive name filter of enabled workflows, capped for the typeahead. */
export function filterWorkflows(workflows: WorkflowOption[], query: string): WorkflowOption[] {
	const q = query.trim().toLowerCase();
	const enabled = workflows.filter((w) => w.is_enabled !== false);
	const matched = q
		? enabled.filter((w) => w.name.toLowerCase().includes(q))
		: enabled;
	return matched.slice(0, MENTION_LIMIT);
}

export function payloadIsEmpty(p: AgentComposerPayload): boolean {
	return !p.content.trim() && p.mentions.length === 0 && p.attachments.length === 0;
}
