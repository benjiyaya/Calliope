<script lang="ts">
	/**
	 * PillPopover — pill that opens a panel with arbitrary content.
	 *
	 * Portaled to document.body and flipped upward near the viewport
	 * bottom so the docked Omni composer does not clip it.
	 */
	import Icon from '$lib/components/ui/Icon.svelte';

	interface Props {
		label: string;
		badge?: number;
		icon?: string;
		children: import('svelte').Snippet;
	}

	let { label, badge = 0, icon, children }: Props = $props();

	let open = $state(false);
	let pillEl = $state<HTMLElement | null>(null);
	let panelEl = $state<HTMLElement | null>(null);
	let menuPos = $state({ top: 0, left: 0, maxHeight: 360 });

	function toggle(e: MouseEvent) {
		e.stopPropagation();
		open = !open;
	}

	function onWindowClick(e: MouseEvent) {
		if (!open) return;
		const t = e.target as Node;
		if (pillEl?.contains(t) || panelEl?.contains(t)) return;
		open = false;
	}

	function onKey(e: KeyboardEvent) {
		if (e.key === 'Escape') open = false;
	}

	function placePanel() {
		if (!pillEl || !panelEl) return;
		const rect = pillEl.getBoundingClientRect();
		const gap = 6;
		const pad = 8;
		const spaceBelow = window.innerHeight - rect.bottom - gap - pad;
		const spaceAbove = rect.top - gap - pad;
		const preferred = Math.min(400, Math.max(panelEl.scrollHeight, 80));
		const openUp = spaceBelow < Math.min(preferred, 180) && spaceAbove > spaceBelow;
		const maxHeight = Math.max(120, openUp ? spaceAbove : spaceBelow);
		const h = Math.min(panelEl.scrollHeight, maxHeight);

		let top = openUp ? rect.top - h - gap : rect.bottom + gap;
		let left = rect.left;
		const width = Math.max(220, panelEl.offsetWidth);

		left = Math.max(pad, Math.min(left, window.innerWidth - width - pad));
		top = Math.max(pad, Math.min(top, window.innerHeight - Math.min(h, maxHeight) - pad));

		menuPos = { top, left, maxHeight };
	}

	$effect(() => {
		if (!open || !panelEl) return;
		document.body.appendChild(panelEl);
		return () => {
			panelEl?.remove();
		};
	});

	$effect(() => {
		if (!open) return;
		const t = window.setTimeout(() => {
			placePanel();
			window.addEventListener('click', onWindowClick);
			window.addEventListener('keydown', onKey);
			window.addEventListener('resize', placePanel);
			window.addEventListener('scroll', placePanel, true);
		}, 0);
		return () => {
			clearTimeout(t);
			window.removeEventListener('click', onWindowClick);
			window.removeEventListener('keydown', onKey);
			window.removeEventListener('resize', placePanel);
			window.removeEventListener('scroll', placePanel, true);
		};
	});

	$effect(() => {
		if (open && panelEl && pillEl) {
			requestAnimationFrame(placePanel);
		}
	});
</script>

<div class="pill-wrap" bind:this={pillEl}>
	<button
		type="button"
		class="pill"
		onclick={toggle}
		aria-haspopup="dialog"
		aria-expanded={open}
	>
		{#if icon}
			<Icon name={icon as never} size={14} />
		{/if}
		<span class="pill-label">{label}</span>
		{#if badge > 0}
			<span class="pill-badge">{badge}</span>
		{/if}
		<Icon name="chevron-down" size={12} />
	</button>
</div>

{#if open}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
		bind:this={panelEl}
		class="popover"
		style="top: {menuPos.top}px; left: {menuPos.left}px; max-height: {menuPos.maxHeight}px"
		onclick={(e) => e.stopPropagation()}
	>
		{@render children()}
	</div>
{/if}

<style>
	.pill-wrap {
		position: relative;
		display: inline-flex;
	}

	.pill {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		height: var(--pill-height, 36px);
		padding: var(--pill-padding, 0 14px);
		border-radius: var(--pill-radius, 9999px);
		background: var(--pill-bg, var(--bg-elevated));
		border: 1px solid var(--pill-border, var(--border));
		color: var(--text-secondary);
		font-size: var(--pill-font-size, 13px);
		font-family: var(--font-body);
		cursor: pointer;
		white-space: nowrap;
		transition:
			border-color 0.15s,
			background 0.15s;
	}

	.pill:hover {
		border-color: var(--text-muted);
		color: var(--text-primary);
	}

	.pill:focus-visible {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 3px var(--accent-glow);
	}

	.pill-label {
		font-weight: 500;
	}

	.pill-badge {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 16px;
		height: 16px;
		padding: 0 4px;
		border-radius: 9999px;
		background: var(--accent);
		color: white;
		font-size: 10px;
		font-weight: 700;
	}

	.popover {
		position: fixed;
		z-index: 10000;
		min-width: 220px;
		max-width: 320px;
		background: var(--bg-surface);
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
		box-shadow:
			0 8px 24px rgba(0, 0, 0, 0.5),
			0 0 0 1px rgba(0, 0, 0, 0.3);
		padding: 16px;
		display: flex;
		flex-direction: column;
		gap: 12px;
		overflow-y: auto;
		overscroll-behavior: contain;
	}
</style>
