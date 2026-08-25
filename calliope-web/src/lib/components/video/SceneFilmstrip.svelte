<script lang="ts">
	/**
	 * SceneFilmstrip — slim horizontal scene strip under the hero monitor.
	 * Never sticky; fixed height; keyboard Left/Right/Home/End.
	 */
	import type { Scene } from '$lib/api';
	import Button from '$lib/components/ui/Button.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';

	type Thumb = { kind: 'image' | 'video'; src: string } | null;

	interface Props {
		scenes: Scene[];
		selectedId: number | null;
		statusOf: (scene: Scene) => string;
		thumbFor: (scene: Scene) => Thumb;
		formatClock: (sec: number) => string;
		chained?: (scene: Scene) => boolean;
		onSelect: (id: number) => void;
		onStep: (dir: -1 | 1) => void;
	}

	let {
		scenes,
		selectedId,
		statusOf,
		thumbFor,
		formatClock,
		chained = () => false,
		onSelect,
		onStep,
	}: Props = $props();

	const selected = $derived(scenes.find((s) => s.id === selectedId) ?? null);
	const selectedIndex = $derived(scenes.findIndex((s) => s.id === selectedId));

	function onKeydown(e: KeyboardEvent) {
		if (scenes.length === 0) return;
		let nextId: number | null = null;
		if (e.key === 'ArrowRight') {
			const i = selectedIndex < 0 ? 0 : selectedIndex + 1;
			nextId = scenes[Math.min(i, scenes.length - 1)]?.id ?? null;
		} else if (e.key === 'ArrowLeft') {
			const i = selectedIndex < 0 ? 0 : selectedIndex - 1;
			nextId = scenes[Math.max(i, 0)]?.id ?? null;
		} else if (e.key === 'Home') nextId = scenes[0]?.id ?? null;
		else if (e.key === 'End') nextId = scenes[scenes.length - 1]?.id ?? null;
		if (nextId == null || nextId === selectedId) return;
		e.preventDefault();
		onSelect(nextId);
		queueMicrotask(() => document.getElementById(`film-clip-${nextId}`)?.focus());
	}
</script>

<section class="strip" aria-label="Scene filmstrip">
	<div
		class="track"
		role="listbox"
		aria-label="Scene clips"
		aria-activedescendant={selectedId != null ? `film-clip-${selectedId}` : undefined}
		tabindex="0"
		onkeydown={onKeydown}
	>
		{#each scenes as scene (scene.id)}
			{@const st = statusOf(scene)}
			{@const thumb = thumbFor(scene)}
			<button
				type="button"
				id="film-clip-{scene.id}"
				role="option"
				aria-selected={selectedId === scene.id}
				class="clip status-{st}"
				class:selected={selectedId === scene.id}
				class:chained={chained(scene)}
				onclick={() => onSelect(scene.id)}
				title={`#${scene.order_index} · id ${scene.id} · ${scene.heading || 'Scene'} · ${formatClock(scene.duration_sec || 5)}`}
			>
				<span class="bar" aria-hidden="true"></span>
				<span class="thumb" aria-hidden="true">
					{#if thumb?.kind === 'image'}
						<img class="thumb-media" src={thumb.src} alt="" loading="lazy" />
					{:else if thumb?.kind === 'video'}
						<!-- svelte-ignore a11y_media_has_caption -->
						<video class="thumb-media" src={thumb.src + '#t=0.1'} muted playsinline preload="metadata"></video>
					{:else}
						<span class="thumb-slate">#{scene.order_index}</span>
					{/if}
				</span>
				<span class="meta">
					<span class="num">#{scene.order_index}</span>
					<span class="sid">id {scene.id}</span>
				</span>
			</button>
		{/each}
	</div>

	<div class="transport">
		<Button variant="ghost" size="sm" disabled={!selected} onclick={() => onStep(-1)}>
			<Icon name="chevron-left" size={14} /> Prev
		</Button>
		<span class="pos">
			{selected ? `#${selected.order_index} of ${scenes.length} · id ${selected.id}` : '—'}
		</span>
		<Button variant="ghost" size="sm" disabled={!selected} onclick={() => onStep(1)}>
			Next <Icon name="chevron-right" size={14} />
		</Button>
	</div>
</section>

<style>
	.strip {
		flex-shrink: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
		padding: 8px 0 0;
	}

	.track {
		display: flex;
		gap: 8px;
		overflow-x: auto;
		padding: 2px 2px 6px;
		min-height: 72px;
		scrollbar-width: thin;
	}

	.clip {
		position: relative;
		flex: 0 0 96px;
		width: 96px;
		height: 72px;
		padding: 0;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: var(--bg-elevated);
		color: var(--text-primary);
		cursor: pointer;
		overflow: hidden;
		display: flex;
		flex-direction: column;
		transition:
			border-color 0.15s,
			box-shadow 0.15s;
	}

	.clip:hover {
		border-color: var(--text-muted);
	}

	.clip.selected {
		border-color: var(--accent);
		box-shadow: 0 0 0 1px var(--accent);
	}

	.clip.chained::before {
		content: '';
		position: absolute;
		left: -6px;
		top: 50%;
		width: 6px;
		height: 2px;
		background: var(--accent);
		transform: translateY(-50%);
		z-index: 2;
	}

	.clip:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}

	.bar {
		position: absolute;
		inset: 0 0 auto 0;
		height: 3px;
		z-index: 1;
		background: #3f3f46;
	}

	.clip.status-done .bar {
		background: var(--success);
	}
	.clip.status-running .bar,
	.clip.status-pending .bar {
		background: var(--warning);
	}
	.clip.status-failed .bar {
		background: var(--error);
	}

	.thumb {
		flex: 1;
		min-height: 0;
		background: var(--bg-primary);
	}

	.thumb-media {
		display: block;
		width: 100%;
		height: 100%;
		object-fit: cover;
		pointer-events: none;
	}

	.thumb-slate {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 100%;
		height: 100%;
		font-family: var(--font-mono);
		font-size: 14px;
		font-weight: 700;
		color: var(--text-muted);
	}

	.meta {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 3px 6px;
		background: var(--bg-surface);
		border-top: 1px solid var(--border);
		flex-shrink: 0;
	}

	.num {
		font-family: var(--font-mono);
		font-size: 10px;
		font-weight: 700;
		color: var(--accent);
	}

	.sid {
		font-family: var(--font-mono);
		font-size: 9px;
		font-weight: 600;
		color: var(--text-muted);
	}

	.transport {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 12px;
	}

	.pos {
		font-size: 12px;
		color: var(--text-secondary);
		min-width: 22ch;
		text-align: center;
	}
</style>
