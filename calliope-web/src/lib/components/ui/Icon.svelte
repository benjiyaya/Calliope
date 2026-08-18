<script lang="ts">
	import { icons, type IconName } from './icons';

	interface Props {
		name: IconName;
		size?: number;
	}

	let { name, size = 18 }: Props = $props();

	const content = $derived(icons[name] ?? '');
</script>

{#if content}
	<svg
		class="icon"
		width={size}
		height={size}
		viewBox="0 0 24 24"
		fill="none"
		stroke="currentColor"
		stroke-width="1.5"
		stroke-linecap="round"
		stroke-linejoin="round"
		aria-hidden="true"
	>
		{@html content}
	</svg>
{/if}

<style>
	.icon {
		display: inline-block;
		vertical-align: -0.125em;
		flex-shrink: 0;
	}
</style>
