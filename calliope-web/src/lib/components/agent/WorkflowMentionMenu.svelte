<script lang="ts">
	interface MenuItem {
		id?: number;
		name: string;
		kind?: string;
		description?: string | null;
	}

	interface Props {
		open: boolean;
		items: MenuItem[];
		activeIndex: number;
		/** Caret / `@` viewport box — menu opens above this (composer is bottom-docked). */
		anchor: { top: number; bottom: number; left: number };
		/** When set, the list is replaced with this explanation (one-workflow guardrail). */
		lockReason?: string | null;
		/** Skill mode: rows show `/name` + a description subtitle, no kind chip. */
		skillMode?: boolean;
		onSelect: (item: any) => void;
		onHover: (index: number) => void;
	}

	let {
		open,
		items,
		activeIndex,
		anchor,
		lockReason = null,
		skillMode = false,
		onSelect,
		onHover,
	}: Props = $props();

	let menuEl = $state<HTMLDivElement | null>(null);
	let pos = $state({ top: 0, left: 0, maxHeight: 280 });
	let placed = $state(false);

	const GAP = 6;
	const PAD = 8;
	const MENU_MAX = 280;

	function placeMenu() {
		if (!menuEl) return;
		const h = Math.min(menuEl.scrollHeight || 40, MENU_MAX);
		const spaceAbove = anchor.top - GAP - PAD;
		const spaceBelow = window.innerHeight - anchor.bottom - GAP - PAD;
		// Bottom-docked composer: prefer above the `@` unless there is no room.
		const openUp = spaceAbove >= Math.min(h, 80) || spaceAbove >= spaceBelow;
		const maxHeight = Math.max(80, Math.min(MENU_MAX, openUp ? spaceAbove : spaceBelow));
		const used = Math.min(h, maxHeight);
		let top = openUp ? anchor.top - used - GAP : anchor.bottom + GAP;
		let left = anchor.left;
		left = Math.max(PAD, Math.min(left, window.innerWidth - 280 - PAD));
		top = Math.max(PAD, Math.min(top, window.innerHeight - used - PAD));
		pos = { top, left, maxHeight };
		placed = true;
	}

	$effect(() => {
		if (!open || !menuEl) return;
		document.body.appendChild(menuEl);
		return () => {
			menuEl?.remove();
		};
	});

	$effect(() => {
		if (!open) {
			placed = false;
			return;
		}
		if (!menuEl) return;
		void items.length;
		void lockReason;
		void anchor.top;
		void anchor.left;
		void anchor.bottom;
		requestAnimationFrame(placeMenu);
	});
</script>

{#if open}
	<div
		bind:this={menuEl}
		class="mention-menu"
		style="top: {pos.top}px; left: {pos.left}px; max-height: {pos.maxHeight}px; visibility: {placed
			? 'visible'
			: 'hidden'}"
		role="listbox"
		aria-label={skillMode ? 'Skills' : 'Workflows'}
	>
		{#if lockReason}
			<div class="empty">{lockReason}</div>
		{:else if items.length === 0}
			<div class="empty">{skillMode ? 'No matching skill' : 'No matching workflow'}</div>
		{:else}
			{#each items as item, i (item.id ?? item.name)}
				<button
					type="button"
					class="item"
					class:active={i === activeIndex}
					role="option"
					aria-selected={i === activeIndex}
					onmousedown={(e) => {
						e.preventDefault();
						onSelect(item);
					}}
					onmouseenter={() => onHover(i)}
				>
					<span class="text">
						<span class="name">{skillMode ? `/${item.name}` : item.name}</span>
						{#if skillMode && item.description}
							<span class="desc">{item.description}</span>
						{/if}
					</span>
				{#if !skillMode}
					<span class="kind">{item.kind}</span>
				{/if}
				</button>
			{/each}
		{/if}
	</div>
{/if}

<style>
	.mention-menu {
		position: fixed;
		z-index: 10000;
		min-width: 240px;
		max-width: 360px;
		max-height: 280px;
		overflow-y: auto;
		background: var(--bg-surface);
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
		box-shadow:
			0 8px 24px rgba(0, 0, 0, 0.5),
			0 0 0 1px rgba(0, 0, 0, 0.3);
		padding: 4px;
	}
	.empty {
		padding: 10px 12px;
		font-size: 13px;
		color: var(--text-muted);
	}
	.item {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 10px;
		width: 100%;
		padding: 8px 12px;
		border: none;
		border-radius: var(--radius-sm);
		background: transparent;
		color: var(--text-primary);
		font-size: 13px;
		font-family: inherit;
		cursor: pointer;
		text-align: left;
	}
	.item:hover,
	.item.active {
		background: var(--bg-elevated);
	}
	.item.active .name {
		color: var(--accent);
	}
	.text {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
	}
	.name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-weight: 500;
	}
	.desc {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 11px;
		color: var(--text-muted);
	}
	.kind {
		flex-shrink: 0;
		font-size: 11px;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
</style>
