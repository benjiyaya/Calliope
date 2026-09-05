<script lang="ts">
	import { page } from '$app/state';

	let { children } = $props();
</script>

<!-- Same-route navigation (/canvas/2 → /canvas/3) reuses the page component,
     which pins `canvasId` and every closure to the old canvas. Re-keying on
     the id param forces a full remount so the graph, queries, and the
     ?session= deep-link handoff all re-initialize. Key on the param only —
     keying on the full URL would remount when the init effect strips
     ?session= via replaceState. -->
{#key page.params.id}
	{@render children()}
{/key}
