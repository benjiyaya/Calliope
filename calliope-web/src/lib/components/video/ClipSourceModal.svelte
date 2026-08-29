<script lang="ts">
	/**
	 * Video source picker for continue scenes — Auto (previous clip),
	 * Upload file, or any other scene's clip in the project timeline.
	 * Replaces the cramped native select.
	 */
	import { assetUrl } from '$lib/api';
	import Button from '$lib/components/ui/Button.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';

	interface ClipSourceOption {
		/** Scene id as string, or the 'auto' / 'upload' sentinels. */
		id: string;
		label: string;
		/** Clip path — renders a video thumbnail when present. */
		path?: string;
	}

	interface Props {
		open?: boolean;
		/** Current source: 'auto' | 'upload' | a scene id from options. */
		value?: string;
		options?: ClipSourceOption[];
		/** Picking Auto or a project clip. */
		onselect: (source: string) => void;
		/** Picking Upload file — caller opens the file dialog and applies the result. */
		onupload: () => void;
	}

	let { open = $bindable(false), value = 'auto', options = [], onselect, onupload }: Props =
		$props();

	function hideBroken(e: Event) {
		const el = e.currentTarget as HTMLElement | null;
		if (el) el.style.display = 'none';
	}

	function pick(source: string) {
		onselect(source);
		open = false;
	}

	function pickUpload() {
		onupload();
		open = false;
	}
</script>

<Modal bind:open title="Video source" size="xl">
	<div class="sources">
		<button
			type="button"
			class="source-card"
			class:selected={value === 'auto'}
			onclick={() => pick('auto')}
		>
			<span class="source-icon"><Icon name="sparkle" size={18} /></span>
			<span class="source-text">
				<span class="source-name">Auto (previous clip)</span>
				<span class="source-desc">Continue from the previous scene's clip</span>
			</span>
			{#if value === 'auto'}
				<span class="check"><Icon name="check" size={12} /></span>
			{/if}
		</button>

		<button
			type="button"
			class="source-card"
			class:selected={value === 'upload'}
			onclick={pickUpload}
		>
			<span class="source-icon"><Icon name="upload" size={18} /></span>
			<span class="source-text">
				<span class="source-name">Upload file</span>
				<span class="source-desc">Pick a video from your computer</span>
			</span>
			{#if value === 'upload'}
				<span class="check"><Icon name="check" size={12} /></span>
			{/if}
		</button>
	</div>

	{#if options.length > 0}
		<p class="section-label">Project clips <span class="count">{options.length}</span></p>
		<div class="grid-wrap">
			<div class="grid">
				{#each options as opt (opt.id)}
					{@const isSelected = value === opt.id}
					<button
						type="button"
						class="clip-card"
						class:selected={isSelected}
						onclick={() => pick(opt.id)}
						title={opt.label}
					>
						<div class="thumb">
							{#if opt.path}
								<!-- svelte-ignore a11y_media_has_caption -->
								<video
									src={assetUrl(opt.path)}
									muted
									playsinline
									preload="metadata"
									onerror={hideBroken}
								></video>
							{:else}
								<Icon name="film" size={22} />
							{/if}
							{#if isSelected}
								<span class="check"><Icon name="check" size={12} /></span>
							{/if}
						</div>
						<span class="name">{opt.label}</span>
					</button>
				{/each}
			</div>
		</div>
	{:else}
		<p class="empty">No clips in the project yet. Generate a clip first, or upload a video file.</p>
	{/if}

	{#snippet footer()}
		<Button variant="ghost" onclick={() => (open = false)}>Cancel</Button>
	{/snippet}
</Modal>

<style>
	.sources {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 12px;
	}

	.source-card {
		position: relative;
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 14px 16px;
		text-align: left;
		font: inherit;
		font-size: 14px;
		color: var(--text-secondary);
		background: var(--bg-elevated);
		border: 1.5px solid var(--border);
		border-radius: var(--radius-md);
		cursor: pointer;
	}

	.source-card:hover {
		border-color: var(--text-muted);
	}

	.source-card:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}

	.source-card.selected {
		border-color: var(--accent);
		box-shadow: 0 0 0 3px var(--accent-glow);
	}

	.source-icon {
		flex-shrink: 0;
		color: var(--text-muted);
	}

	.source-card.selected .source-icon {
		color: var(--accent);
	}

	.source-text {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
	}

	.source-name {
		font-size: 14px;
		font-weight: 600;
		color: var(--text-primary);
	}

	.source-desc {
		font-size: 12.5px;
		line-height: 1.4;
		color: var(--text-muted);
	}

	.section-label {
		margin: 16px 0 8px;
		font-size: 12px;
		font-weight: 600;
		color: var(--text-secondary);
	}

	.count {
		margin-left: 4px;
		font-weight: 500;
		color: var(--text-muted);
		font-variant-numeric: tabular-nums;
	}

	.grid-wrap {
		overflow-y: auto;
		overscroll-behavior: contain;
		max-height: min(52vh, 560px);
	}

	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
		gap: 12px;
	}

	.clip-card {
		display: flex;
		flex-direction: column;
		gap: 6px;
		padding: 0;
		border: none;
		background: transparent;
		color: inherit;
		font: inherit;
		text-align: left;
		cursor: pointer;
	}

	.thumb {
		position: relative;
		aspect-ratio: 16 / 9;
		border-radius: var(--radius-md);
		border: 1.5px solid var(--border);
		background: var(--bg-elevated);
		overflow: hidden;
		display: flex;
		align-items: center;
		justify-content: center;
		color: var(--text-muted);
	}

	.clip-card:hover .thumb {
		border-color: var(--text-muted);
	}

	.clip-card.selected .thumb {
		border-color: var(--accent);
		box-shadow: 0 0 0 3px var(--accent-glow);
	}

	.clip-card:focus-visible .thumb {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}

	.thumb video {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}

	.check {
		position: absolute;
		top: 6px;
		right: 6px;
		width: 20px;
		height: 20px;
		border-radius: 9999px;
		background: var(--accent);
		color: #fff;
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.name {
		font-size: 13px;
		color: var(--text-secondary);
		line-height: 1.35;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		padding: 0 2px;
	}

	.clip-card.selected .name {
		color: var(--text-primary);
		font-weight: 600;
	}

	.empty {
		margin: 32px 8px;
		text-align: center;
		color: var(--text-muted);
		font-size: 13px;
		line-height: 1.45;
	}
</style>
