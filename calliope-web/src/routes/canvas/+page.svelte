<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { agentApi, canvasApi } from '$lib/api';

	let failed = $state('');

	onMount(async () => {
		try {
			const sessions = await agentApi.listSessions();
			const stored = Number(localStorage.getItem('calliope.agents.activeSession'));
			const found =
				sessions.find((s) => s.id === stored) ?? sessions[0] ?? (await agentApi.createSession({}));
			const graph = found.project_id
				? await canvasApi.ensureForProject(found.project_id)
				: await canvasApi.ensureForSession(found.id);
			goto(`/canvas/${graph.canvas.id}?session=${found.id}`, { replaceState: true });
		} catch (err) {
			failed = err instanceof Error ? err.message : 'Could not open canvas';
		}
	});
</script>

{#if failed}
	<div class="redirect-error" role="alert">{failed}</div>
{:else}
	<div class="redirecting">Opening canvas…</div>
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
