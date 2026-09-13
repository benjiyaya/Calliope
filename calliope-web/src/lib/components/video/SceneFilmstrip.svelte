<script module lang="ts">
	import type { Clip, Scene } from '$lib/api';

	/** One entry in the strip: a renderable clip pinned to its parent scene. */
	export interface FilmstripClip {
		clip: Clip;
		scene: Scene;
		index: number;
		label: string;
	}
</script>

<script lang="ts">
	/**
	 * SceneFilmstrip — slim horizontal strip under the hero monitor.
	 * Renders CLIPS (playback order, `#3.2` labels) with scene-divider grouping.
	 * Never sticky; fixed height; keyboard Left/Right/Home/End.
	 */
	import Button from '$lib/components/ui/Button.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';
	import { t } from '$lib/i18n.svelte';

	type Thumb = { kind: 'image' | 'video'; src: string } | null;

	interface Props {
		clips: FilmstripClip[];
		selectedClipId: number | null;
		statusOfClip: (clipId: number) => string;
		thumbForClip: (clipId: number) => Thumb;
		formatClock: (sec: number) => string;
		onSelectClip: (clipId: number) => void;
		onStep: (dir: -1 | 1) => void;
	}

	let {
		clips,
		selectedClipId,
		statusOfClip,
		thumbForClip,
		formatClock,
		onSelectClip,
		onStep,
	}: Props = $props();

	const selectedLabel = $derived(
		clips.find((c) => c.clip.id === selectedClipId)?.label ?? '—',
	);

	function onKeydown(e: KeyboardEvent) {
		if (clips.length === 0) return;
		const idx = clips.findIndex((c) => c.clip.id === selectedClipId);
		let nextId: number | null = null;
		if (e.key === 'ArrowRight') {
			const i = idx < 0 ? 0 : idx + 1;
			nextId = clips[Math.min(i, clips.length - 1)].clip.id;
		} else if (e.key === 'ArrowLeft') {
			const i = idx < 0 ? 0 : idx - 1;
			nextId = clips[Math.max(i, 0)].clip.id;
		} else if (e.key === 'Home') nextId = clips[0].clip.id;
		else if (e.key === 'End') nextId = clips[clips.length - 1].clip.id;
		if (nextId == null || nextId === selectedClipId) return;
		e.preventDefault();
		onSelectClip(nextId);
		queueMicrotask(() => document.getElementById(`film-clip-${nextId}`)?.focus());
	}
</script>

<section class="strip" aria-label={t('filmstrip.title')}>
	<div
		class="track"
		role="listbox"
		aria-label={t('filmstrip.sceneClips', { n: clips.length })}
		aria-activedescendant={selectedClipId != null ? `film-clip-${selectedClipId}` : undefined}
		tabindex="0"
		onkeydown={onKeydown}
	>
		{#each clips as entry, i (entry.clip.id)}
			{@const st = statusOfClip(entry.clip.id)}
			{@const thumb = thumbForClip(entry.clip.id)}
			{#if entry.index === 0 && i > 0}
				<span class="divider" role="presentation" title={entry.scene.heading || t('filmstrip.scene')}></span>
			{/if}
			<button
				type="button"
				id="film-clip-{entry.clip.id}"
				role="option"
				aria-selected={selectedClipId === entry.clip.id}
				class="clip status-{st}"
				class:selected={selectedClipId === entry.clip.id}
				class:chained={entry.index > 0}
				onclick={() => onSelectClip(entry.clip.id)}
title={t('filmstrip.clipTitle', {
				scene: `${entry.scene.order_index} · ${entry.scene.heading || t('filmstrip.scene')}`,
				shot: entry.index + 1,
				time: formatClock(entry.clip.duration_sec || 5),
			}) + (entry.clip.description ? `\n${entry.clip.description}` : '')}
			>
				<span class="bar" aria-hidden="true"></span>
				<span class="thumb" aria-hidden="true">
					{#if thumb?.kind === 'image'}
						<img class="thumb-media" src={thumb.src} alt="" loading="lazy" />
					{:else if thumb?.kind === 'video'}
						<!-- svelte-ignore a11y_media_has_caption -->
						<video class="thumb-media" src={thumb.src + '#t=0.1'} muted playsinline preload="metadata"></video>
					{:else}
						<span class="thumb-slate">{entry.label}</span>
					{/if}
				</span>
				<span class="meta">
					<span class="num">{entry.label}</span>
					<span class="sid">{formatClock(entry.clip.duration_sec || 5)}</span>
				</span>
			</button>
		{/each}
	</div>

	<div class="transport">
		<Button variant="ghost" size="sm" onclick={() => onStep(-1)}>
			<Icon name="chevron-left" size={14} /> {t('filmstrip.prev')}
		</Button>
		<span class="pos">{selectedLabel}</span>
		<Button variant="ghost" size="sm" onclick={() => onStep(1)}>
			{t('filmstrip.next')} <Icon name="chevron-right" size={14} />
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
		align-items: stretch;
		overflow-x: auto;
		padding: 2px 2px 6px;
		min-height: 72px;
		scrollbar-width: thin;
	}

	.divider {
		flex: 0 0 2px;
		align-self: stretch;
		background: color-mix(in srgb, var(--border) 60%, transparent);
		border-radius: 1px;
		margin: 4px 2px;
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

	/* Shots after the first in a scene: connector nub on the left. */
	.clip.chained::before {
		content: '';
		position: absolute;
		left: -6px;
		top: 50%;
		width: 6px;
		height: 2px;
		background: color-mix(in srgb, var(--text-muted) 60%, transparent);
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
	.clip.status-partial .bar {
		background: color-mix(in srgb, var(--success) 55%, var(--warning));
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
		font-size: 12px;
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
