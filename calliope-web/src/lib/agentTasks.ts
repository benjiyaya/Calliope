/**
 * Canonical deep-link contract between the Project stages and the Agent chat.
 *
 * The Story ("Draft Storyline") and Script ("Regenerate Script") actions now
 * hand off to a fresh, project-linked agent session with the composer
 * pre-filled. Keeping the task → prompt mapping in one place means the stage
 * buttons stay dumb and the wording never drifts out of sync.
 */

export type AgentTaskKind = 'story' | 'script';

/** The pre-filled composer prompt for each task. */
export const AGENT_TASK_PROMPTS: Record<AgentTaskKind, string> = {
	story:
		'Draft the storyline for this project — generate the beats, characters, environments, and any misc. items from the story idea.',
	script:
		'Regenerate the full script for this project — turn the storyline into ordered scenes with action and dialogue.',
};

/** A sidebar-friendly title for the auto-created session. */
export const AGENT_TASK_TITLES: Record<AgentTaskKind, string> = {
	story: 'Draft Storyline',
	script: 'Regenerate Script',
};

export function isAgentTaskKind(value: string | null | undefined): value is AgentTaskKind {
	return value === 'story' || value === 'script';
}

/** Build the /agents deep-link URL for a project-scoped task. */
export function agentDeepLink(projectId: number, task: AgentTaskKind): string {
	const params = new URLSearchParams({ project: String(projectId), task });
	return `/agents?${params.toString()}`;
}
