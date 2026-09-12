<script lang="ts">
	/**
	 * PromptPreviewModal — HITL review gate before Generate (issue #27).
	 * Resolves the exact prompt (saved fresh draft → LLM rewrite → fallback),
	 * lets the user edit/regenerate/save it, and only enqueues on confirm.
	 * Clip-addressed when the project is expanded; falls back to scene
	 * addressing (backend resolves the scene's default clip) otherwise.
	 */
	import { createMutation } from '@tanstack/svelte-query';
	import { toast } from '$lib/toast';
	import { jobsApi, projects, type Clip, type Scene, type Workflow } from '$lib/api';
	import Button from '$lib/components/ui/Button.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';
	import Spinner from '$lib/components/ui/Spinner.svelte';
	import { t } from '$lib/i18n.svelte';

	interface Props {
		open?: boolean;
		projectId: number;
		/** Clip addressing: the exact render unit to preview (backend contract). */
		clip?: Clip | null;
		/** Fallback when un-expanded (backend resolves clip #1 of the scene). */
		scene: Scene | null;
		workflow?: Workflow | null;
		/** Extra form values to pass through on confirm. */
		inputValues?: Record<string, string | number>;
		/** Fallback enqueue path when the modal confirms. */
		onConfirm: (prompt: string) => void;
		onclose?: () => void;
	}

	let {
		open = $bindable(false),
		projectId,
		clip = null,
		scene,
		workflow = null,
		inputValues = {},
		onConfirm,
		onclose,
	}: Props = $props();

	let text = $state('');
	let basedOn = $state('');
	let fromDraft = $state(false);
	let stale = $state(false);
	/** Clip/scene key whose resolve already fired once — guards re-runs. */
	let attemptedFor = $state<number | null>(null);
	/** Resolve failed — modal shows a client-side prose fallback instead of a dead end. */
	let failed = $state(false);

	const preview = createMutation({
		mutationFn: async () => {
			if (!clip && !scene) throw new Error(t('promptPreview.noClipSelected'));
			return jobsApi.previewPrompt(projectId, {
				clip_id: clip?.id,
				scene_id: clip ? undefined : scene?.id,
				workflow_id: workflow?.id,
			});
		},
		onSuccess: (data) => {
			text = data.prompt;
			basedOn = data.based_on;
			fromDraft = data.from_draft;
			failed = false;
			stale = false;
		},
		onError: (err) => {
			failed = true;
			if (scene) {
				// Never a dead end: populate the editor with raw scene text so the
				// user can edit and Generate (confirm sends it via prompts override),
				// or hit Regenerate to retry the rewrite.
				text = proseFallback(scene, clip);
				basedOn = '';
				fromDraft = false;
			}
			toast.error(err instanceof Error ? err.message : String(err));
		},
	});

	// Resolve once per clip (or scene for legacy rows). `attemptedFor` is set
	// before mutating so mutation-store transitions (pending → success/error)
	// can't re-trigger this effect — the old `text` guard fired duplicate
	// requests while the first was still pending.
	$effect(() => {
		if (!open || (!clip && !scene)) return;
		const key = clip?.id ?? -(scene?.id ?? 0);
		if (attemptedFor === key) return;
		attemptedFor = key;
		$preview.mutate();
	});

	/** Fallback body when resolve fails: the clip's beat or the scene prose. */
	function proseFallback(s: Scene, c: Clip | null): string {
		if (c?.description) {
			const heading = (s.heading || '').trim();
			return [heading, c.description.trim()].filter(Boolean).join('\n\n');
		}
		const heading = (s.heading || '').trim();
		const action = (s.action || '').trim();
		const dialog = (s.dialog || '').trim();
		return [heading, action, dialog].filter(Boolean).join('\n\n');
	}

	// Stale check: a draft saved against different content should warn.
	$effect(() => {
		if (!clip || !basedOn) {
			if (!clip) stale = false;
			return;
		}
		const meta = clip.video_settings?.prompt_draft_meta?.based_on;
		stale = fromDraft && meta != null && meta !== basedOn;
	});

	async function saveDraft() {
		if (!text.trim()) return;
		const meta = { based_on: basedOn, saved_at: new Date().toISOString() };
		if (clip) {
			const next = {
				...(clip.video_settings ?? {}),
				prompt_draft: text,
				prompt_draft_meta: meta,
			};
			try {
				await projects.updateClip(projectId, clip.id, { video_settings: next });
				toast.success(t('promptPreview.draftSaved'));
			} catch (err) {
				toast.error(err instanceof Error ? err.message : String(err));
			}
			return;
		}
		if (!scene) return;
		const next = {
			...(scene.video_settings ?? {}),
			prompt_draft: text,
			prompt_draft_meta: meta,
		};
		try {
			await projects.updateScene(projectId, scene.id, { video_settings: next });
			toast.success(t('promptPreview.draftSaved'));
		} catch (err) {
			toast.error(err instanceof Error ? err.message : String(err));
		}
	}

	function regenerate() {
		failed = false;
		$preview.mutate();
	}

	function confirmGenerate() {
		const prompt = text.trim();
		if (!prompt) {
			toast.error(t('promptPreview.promptEmpty'));
			return;
		}
		open = false;
		onConfirm(prompt);
	}
</script>

<Modal bind:open {onclose} title={t('promptPreview.title')} size="lg">
	{#if !clip && !scene}
		<p class="muted">{t('promptPreview.noClip')}</p>
	{:else if $preview.isPending}
		<div class="loading">
			<Spinner size="md" />
			<span>{t('promptPreview.resolving', { suffix: workflow?.prompt_profile === 'minimax_h3_ref' ? t('promptPreview.h3Rewrite') : '' })}</span>
		</div>
	{:else}
		<div class="head-row">
			<span class="meta">
				{#if clip}
					{t('promptPreview.shotMeta', {
						label: clip.label ?? `#${scene?.order_index ?? ''}`,
						heading: scene?.heading || t('promptPreview.untitled'),
					})}
				{:else}
					{t('promptPreview.sceneMeta', {
						index: scene?.order_index ?? '',
						heading: scene?.heading || t('promptPreview.untitled'),
					})}
				{/if}
			</span>
			<span class="meta">{workflow?.name ?? t('promptPreview.defaultWorkflow')}</span>
		</div>

		{#if stale}
			<div class="stale-hint" role="status">
				<Icon name="alert" size={14} />
				<span>{t('promptPreview.staleHint')}</span>
			</div>
		{/if}
		<textarea
			class="prompt-editor"
			bind:value={text}
			rows={16}
			spellcheck="false"
			aria-label={t('promptPreview.editorAria')}
		></textarea>

		{#if failed}
			<div class="stale-hint" role="status">
				<Icon name="alert" size={14} />
				<span>{t('promptPreview.failedHint')}</span>
			</div>
		{:else if fromDraft}
			<p class="hint">{t('promptPreview.fromDraftHint')}</p>
		{:else if workflow?.prompt_profile === 'minimax_h3_ref'}
			<p class="hint">{t('promptPreview.h3Hint')}</p>
		{:else}
			<p class="hint">{t('promptPreview.proseHint')}</p>
		{/if}
	{/if}

	{#snippet footer()}
		<Button variant="ghost" onclick={() => (open = false)}>{t('common.cancelButton')}</Button>
		<Button variant="secondary" disabled={$preview.isPending || !text} onclick={saveDraft}>
			{t('promptPreview.saveDraft')}
		</Button>
		<Button variant="secondary" disabled={$preview.isPending} onclick={regenerate}>
			<Icon name="retry" size={14} /> {t('promptPreview.regenerate')}
		</Button>
		<Button variant="primary" disabled={$preview.isPending || !text} onclick={confirmGenerate}>
			<Icon name="play" size={14} /> {t('promptPreview.generate')}
		</Button>
	{/snippet}
</Modal>

<style>
	.muted {
		margin: 0;
		font-size: 13px;
		color: var(--text-secondary);
	}

	.loading {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 24px 0;
		font-size: 13px;
		color: var(--text-secondary);
	}

	.head-row {
		display: flex;
		justify-content: space-between;
		gap: 12px;
		margin: 0 0 10px;
	}

	.meta {
		font-family: var(--font-mono);
		font-size: 12px;
		color: var(--text-muted);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.stale-hint {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 0 0 10px;
		padding: 8px 10px;
		font-size: 13px;
		color: var(--warning);
		border: 1px solid color-mix(in srgb, var(--warning) 40%, var(--border));
		border-radius: var(--radius-sm);
		background: color-mix(in srgb, var(--warning) 10%, var(--bg-surface));
	}

	.stale-hint :global(svg) {
		flex-shrink: 0;
	}

	.prompt-editor {
		width: 100%;
		min-height: 260px;
		padding: 12px;
		font-family: var(--font-mono);
		font-size: 12px;
		line-height: 1.55;
		color: var(--text-primary);
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
		resize: vertical;
	}

	.prompt-editor:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 1px;
	}

	.hint {
		margin: 8px 0 0;
		font-size: 12px;
		color: var(--text-muted);
	}
</style>
