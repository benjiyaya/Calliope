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
	mentions: (WorkflowMention | SkillMention)[];
	attachments: AgentAttachment[];
	/** question/asked seq this message answers (question-card click). */
	answer_to?: number;
}

export interface WorkflowOption {
	id: number;
	name: string;
	kind: WorkflowKind;
	description?: string | null;
	is_enabled?: boolean;
}

export interface SkillOption {
	name: string;
	description: string;
	tags: string[];
}

/** A `/skill` command inserted as a chip. Serialized into the message's
 * mentions payload so the backend can project it into the LLM context. */
export interface SkillMention {
	type: 'skill';
	name: string;
	description: string;
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

/** Case-insensitive filter of skills for the `/` typeahead (name + description). */
export function filterSkills(skills: SkillOption[], query: string): SkillOption[] {
	const q = query.trim().toLowerCase();
	const matched = q
		? skills.filter(
				(s) =>
					s.name.toLowerCase().includes(q) || s.description.toLowerCase().includes(q),
			)
		: skills;
	return matched.slice(0, MENTION_LIMIT);
}

export function payloadIsEmpty(p: AgentComposerPayload): boolean {
	return !p.content.trim() && p.mentions.length === 0 && p.attachments.length === 0;
}
