<script lang="ts">
	/**
	 * PillSelect — pill-shaped dropdown for the Omni control bar.
	 *
	 * Menu is portaled to document.body so overflow:hidden ancestors
	 * (composer shell) cannot clip it. Opens upward when there isn't
	 * room below the pill (bottom-docked composer).
	 */
	import Icon from '$lib/components/ui/Icon.svelte';

	interface Option {
		value: string | number;
		label: string;
	}

	interface Props {
		label: string;
		options: Option[];
		value: string | number | null;
		onchange: (value: string | number) => void;
		icon?: string;
		highlight?: boolean;
	}

	let {
		label,
		options,
		value,
		onchange,
		icon,
		highlight = false,
	}: Props = $props();

	let open = $state(false);
	let pillEl = $state<HTMLElement | null>(null);
	let menuEl = $state<HTMLElement | null>(null);
	let menuPos = $state({ top: 0, left: 0, maxHeight: 320, minWidth: 160 });

	function toggle(e: MouseEvent) {
		e.stopPropagation();
		open = !open;
	}

	function select(val: string | number) {
		onchange(val);
		open = false;
	}

	function onWindowClick(e: MouseEvent) {
		if (!open) return;
		const t = e.target as Node;
		if (pillEl?.contains(t) || menuEl?.contains(t)) return;
		open = false;
	}

	function onKey(e: KeyboardEvent) {
		if (e.key === 'Escape') open = false;
	}

	function placeMenu() {
		if (!pillEl || !menuEl) return;
		const rect = pillEl.getBoundingClientRect();
		const gap = 6;
		const pad = 8;
		const spaceBelow = window.innerHeight - rect.bottom - gap - pad;
		const spaceAbove = rect.top - gap - pad;
		const preferred = Math.min(360, Math.max(menuEl.scrollHeight, 40));
		const openUp = spaceBelow < Math.min(preferred, 200) && spaceAbove > spaceBelow;
		const maxHeight = Math.max(120, openUp ? spaceAbove : spaceBelow);

		// Measure after max-height applied next frame — use preferred for top calc
		const h = Math.min(menuEl.scrollHeight, maxHeight);
		let top = openUp ? rect.top - h - gap : rect.bottom + gap;
		let left = rect.left;
		const minWidth = Math.max(160, rect.width);

		left = Math.max(pad, Math.min(left, window.innerWidth - minWidth - pad));
		top = Math.max(pad, Math.min(top, window.innerHeight - Math.min(h, maxHeight) - pad));

		menuPos = { top, left, maxHeight, minWidth };
	}

	// Portal menu to <body> so composer overflow cannot clip it
	$effect(() => {
		if (!open || !menuEl) return;
		document.body.appendChild(menuEl);
		return () => {
			menuEl?.remove();
		};
	});

	$effect(() => {
		if (!open) return;
		const t = window.setTimeout(() => {
			placeMenu();
			window.addEventListener('click', onWindowClick);
			window.addEventListener('keydown', onKey);
			window.addEventListener('resize', placeMenu);
			window.addEventListener('scroll', placeMenu, true);
		}, 0);
		return () => {
			clearTimeout(t);
			window.removeEventListener('click', onWindowClick);
			window.removeEventListener('keydown', onKey);
			window.removeEventListener('resize', placeMenu);
			window.removeEventListener('scroll', placeMenu, true);
		};
	});

	// Re-place once menuEl binds / options change
	$effect(() => {
		if (open && menuEl && pillEl) {
			void options.length;
			requestAnimationFrame(placeMenu);
		}
	});
</script>

<div class="pill-wrap" bind:this={pillEl}>
	<button
		type="button"
		class="pill"
		class:highlight
		onclick={toggle}
		aria-haspopup="listbox"
		aria-expanded={open}
	>
		{#if icon}
			<Icon name={icon as never} size={14} />
		{/if}
		<span class="pill-label">{label}</span>
		<Icon name="chevron-down" size={12} />
	</button>
</div>

{#if open}
	<div
		bind:this={menuEl}
		class="pill-menu"
		style="top: {menuPos.top}px; left: {menuPos.left}px; max-height: {menuPos.maxHeight}px; min-width: {menuPos.minWidth}px"
		role="listbox"
	>
		{#each options as opt (opt.value)}
			<button
				type="button"
				class="menu-item"
				class:active={opt.value === value}
				role="option"
				aria-selected={opt.value === value}
				onclick={() => select(opt.value)}
			>
				<span class="item-label">{opt.label}</span>
				{#if opt.value === value}
					<Icon name="check" size={14} />
				{/if}
			</button>
		{/each}
	</div>
{/if}

<style>
	.pill-wrap {
		position: relative;
		display: inline-flex;
		max-width: 100%;
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
		color: var(--text-primary);
		font-size: var(--pill-font-size, 13px);
		font-family: var(--font-body);
		cursor: pointer;
		max-width: 100%;
		transition:
			border-color 0.15s,
			background 0.15s;
	}

	.pill:hover {
		border-color: var(--text-muted);
	}

	.pill:focus-visible {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 3px var(--accent-glow);
	}

	.pill.highlight {
		border-color: var(--accent);
		color: var(--accent);
	}

	.pill-label {
		font-weight: 500;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 280px;
	}

	.pill-menu {
		position: fixed;
		z-index: 10000;
		background: var(--bg-surface);
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
		box-shadow:
			0 8px 24px rgba(0, 0, 0, 0.5),
			0 0 0 1px rgba(0, 0, 0, 0.3);
		padding: 4px;
		display: flex;
		flex-direction: column;
		gap: 2px;
		overflow-y: auto;
		overscroll-behavior: contain;
	}

	.menu-item {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		padding: 8px 12px;
		border-radius: var(--radius-sm);
		background: transparent;
		border: none;
		color: var(--text-secondary);
		font-size: 13px;
		font-family: var(--font-body);
		cursor: pointer;
		text-align: left;
		width: 100%;
	}

	.item-label {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.menu-item:hover {
		background: var(--bg-elevated);
		color: var(--text-primary);
	}

	.menu-item.active {
		color: var(--accent);
		font-weight: 600;
	}
</style>
