<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { agentApi, canvasApi } from '$lib/api';
	import { AGENT_TASK_PROMPTS, AGENT_TASK_TITLE_KEYS, isAgentTaskKind } from '$lib/agentTasks';
	import { t } from '$lib/i18n.svelte';

	let failed = $state('');

	onMount(async () => {
		try {
			const projectParam = page.url.searchParams.get('project');
			const taskParam = page.url.searchParams.get('task');
			let sessionId: number;
			let prefill = '';

			if (projectParam != null || taskParam != null) {
				// Deep-link contract from the Project stages: /agents?project=<id>&task=<story|script>.
				// Reuse the project's session for this task when one exists — every
				// click used to create a fresh session and flooded the rail.
				const pid = projectParam != null && projectParam !== '' ? Number(projectParam) : null;
				const task = isAgentTaskKind(taskParam) ? taskParam : null;
				let reused: number | null = null;
				if (task && pid != null && Number.isFinite(pid)) {
					const mapKey = `calliope.agentTask.${pid}.${task}`;
					const list = await agentApi.listSessions(pid);
					const storedId = Number(localStorage.getItem(mapKey));
					const wanted = t(AGENT_TASK_TITLE_KEYS[task]);
					const found =
						list.find((s) => s.id === storedId) ??
						list.find((s) => s.title === wanted);
					if (found) {
						reused = found.id;
					} else {
						const s = await agentApi.createSession({
							project_id: pid,
							title: wanted,
						});
						localStorage.setItem(mapKey, String(s.id));
						reused = s.id;
					}
				}
				if (reused != null) {
					sessionId = reused;
				} else {
					const s = await agentApi.createSession({
						...(pid != null && Number.isFinite(pid) ? { project_id: pid } : {}),
						...(task ? { title: t(AGENT_TASK_TITLE_KEYS[task]) } : {}),
					});
					sessionId = s.id;
				}
				prefill = task ? AGENT_TASK_PROMPTS[task] : '';
			} else {
				const sessions = await agentApi.listSessions();
				const stored = Number(localStorage.getItem('calliope.agents.activeSession'));
				const found = sessions.find((s) => s.id === stored) ?? sessions[0];
				sessionId = found?.id ?? (await agentApi.createSession({})).id;
			}

			const session = await agentApi.getSession(sessionId);
			const graph = session.project_id
				? await canvasApi.ensureForProject(session.project_id)
				: await canvasApi.ensureForSession(session.id);
			const url = `/canvas/${graph.canvas.id}?session=${session.id}`;
			if (prefill) sessionStorage.setItem('calliope.canvas.composerPrefill', prefill);
			goto(url, { replaceState: true });
		} catch (err) {
			failed = err instanceof Error ? err.message : t('agents.openFailed');
		}
	});
</script>

{#if failed}
	<div class="redirect-error" role="alert">{failed}</div>
{:else}
	<div class="redirecting">{t('agents.opening')}</div>
{/if}

<style>
	.redirecting,
	.redirect-error {
		display: flex;
		align-items: center;
		justify-content: center;
		height: 100vh;
		color: var(--text-secondary);
		font-size: 14px;
	}
	.redirect-error {
		color: #f87171;
	}
</style>
