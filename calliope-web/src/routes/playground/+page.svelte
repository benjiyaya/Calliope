<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { agentApi, canvasApi } from '$lib/api';

	let failed = $state('');

	onMount(async () => {
		try {
			const sessions = await agentApi.listSessions();
			const sandbox = sessions.find((s) => s.project_id == null);
			const s = sandbox ?? (await agentApi.createSession({}));
			const graph = await canvasApi.ensureForSession(s.id);
			goto(`/canvas/${graph.canvas.id}?session=${s.id}`, { replaceState: true });
		} catch (err) {
			failed = err instanceof Error ? err.message : 'Could not open sandbox canvas';
		}
	});
</script>

{#if failed}
	<div class="redirect-error" role="alert">{failed}</div>
{:else}
	<div class="redirecting">Opening sandbox canvas…</div>
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
