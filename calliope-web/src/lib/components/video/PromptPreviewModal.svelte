<script lang="ts">
	/**
	 * PromptPreviewModal — HITL review gate before Generate (issue #27).
	 * Resolves the exact prompt (saved fresh draft → LLM rewrite → fallback),
	 * lets the user edit/regenerate/save it, and only enqueues on confirm.
	 */
	import { createMutation } from '@tanstack/svelte-query';
	import { toast } from '$lib/toast';
	import { jobsApi, projects, type Scene, type Workflow } from '$lib/api';
	import Button from '$lib/components/ui/Button.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';
	import Spinner from '$lib/components/ui/Spinner.svelte';

	interface Props {
		open?: boolean;
		projectId: number;
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
	/** Scene id whose resolve already fired once — guards against store-transition re-runs. */
	let attemptedFor = $state<number | null>(null);
	/** Resolve failed — modal shows a client-side prose fallback instead of a dead end. */
	let failed = $state(false);

	const preview = createMutation({
		mutationFn: async () => {
			if (!scene) throw new Error('No scene selected');
			return jobsApi.previewPrompt(projectId, {
				scene_id: scene.id,
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
				text = proseFallback(scene);
				basedOn = '';
				fromDraft = false;
			}
			toast.error(err instanceof Error ? err.message : String(err));
		},
	});

	// Resolve once per scene. `attemptedFor` is set before mutating so
	// mutation-store transitions (pending → success/error) can't re-trigger
	// this effect — the old `text` guard fired duplicate requests while the
	// first was still pending.
	$effect(() => {
		if (!open || !scene) return;
		if (attemptedFor === scene.id) return;
		attemptedFor = scene.id;
		$preview.mutate();
	});

	function proseFallback(s: Scene): string {
		const heading = (s.heading || '').trim();
		const action = (s.action || '').trim();
		const dialog = (s.dialog || '').trim();
		return [heading, action, dialog].filter(Boolean).join('\n\n');
	}

	// Stale check: a draft saved against different scene content should warn.
	$effect(() => {
		if (!scene || !basedOn) return;
		const meta = scene.video_settings?.prompt_draft_meta?.based_on;
		stale = fromDraft && meta != null && meta !== basedOn;
	});

	const draftMeta = $derived(scene?.video_settings?.prompt_draft_meta);

	async function saveDraft() {
		if (!scene || !text.trim()) return;
		const existing = scene.video_settings ?? {};
		const next = {
			...existing,
			prompt_draft: text,
			prompt_draft_meta: { based_on: basedOn, saved_at: new Date().toISOString() },
		};
		try {
			await projects.updateScene(projectId, scene.id, { video_settings: next });
			toast.success('Draft saved — Generate will use it');
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
			toast.error('Prompt is empty — edit or regenerate before generating');
			return;
		}
		open = false;
		onConfirm(prompt);
	}
</script>

<Modal bind:open {onclose} title="Review prompt before generating" size="lg">
	{#if !scene}
		<p class="muted">No scene selected.</p>
	{:else if $preview.isPending}
		<div class="loading">
			<Spinner size="md" />
			<span>Resolving prompt{workflow?.prompt_profile === 'minimax_h3_ref' ? ' (H3 rewrite)' : ''}…</span>
		</div>
	{:else}
		<div class="head-row">
			<span class="meta">Scene #{scene.order_index} · {scene.heading || 'Untitled'}</span>
			<span class="meta">{workflow?.name ?? 'Default workflow'}</span>
		</div>

		{#if stale}
			<div class="stale-hint" role="status">
				<Icon name="alert" size={14} />
				<span>Saved draft is based on older scene content — regenerate to refresh it.</span>
			</div>
		{/if}

		<textarea
			class="prompt-editor"
			bind:value={text}
			rows={16}
			spellcheck="false"
			aria-label="Prompt text sent to the workflow"
		></textarea>

		{#if failed}
			<div class="stale-hint" role="status">
				<Icon name="alert" size={14} />
				<span>Rewrite unavailable — showing raw scene text. You can edit and Generate, or Retry.</span>
			</div>
		{:else if fromDraft}
			<p class="hint">Loaded from your saved draft. Regenerate re-runs the rewrite.</p>
		{:else if workflow?.prompt_profile === 'minimax_h3_ref'}
			<p class="hint">MiniMax H3 six-section rewrite. Edit freely — this exact text goes to the (Input:prompt) node.</p>
		{:else}
			<p class="hint">Scene prompt (prose profile). Edit freely before generating.</p>
		{/if}
	{/if}

	{#snippet footer()}
		<Button variant="ghost" onclick={() => (open = false)}>Cancel</Button>
		<Button variant="secondary" disabled={$preview.isPending || !text} onclick={saveDraft}>
			Save draft
		</Button>
		<Button variant="secondary" disabled={$preview.isPending} onclick={regenerate}>
			<Icon name="retry" size={14} /> Regenerate
		</Button>
		<Button variant="primary" disabled={$preview.isPending || !text} onclick={confirmGenerate}>
			<Icon name="play" size={14} /> Generate
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
