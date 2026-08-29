<script lang="ts">
	/**
	 * OmniComposer — Kling AI / MiniMax H3 style unified composer.
	 *
	 * Replaces ComfyDynamicForm in contexts that benefit from the compact
	 * Omni layout (Playground, QueueStage video scenes).
	 *
	 * Takes the same `ComfyDynamicInput[]` and `values` record, classifies
	 * each input into a UI zone (composer / media / control / advanced),
	 * and renders the appropriate compact widget per zone.
	 *
	 * The values contract is identical to ComfyDynamicForm — picking 1080p
	 * from the resolution pill writes `values[widthNodeId] = 1920` and
	 * `values[heightNodeId] = 1080`. Backend API is untouched.
	 */
	import type { ComfyDynamicInput } from '$lib/comfy/types';
	import { classifyAll, RESOLUTION_PRESETS, resolutionLabel } from '$lib/comfy/classifyInput';
	import { createUploadManager } from '$lib/comfy/useUpload.svelte';
	import { normalizeInputRole } from '$lib/comfy/parser';
	import type { AssetOption } from '$lib/assetPicker';
	import { assetUrl } from '$lib/api';
	import Icon from '$lib/components/ui/Icon.svelte';
	import PillSelect from './omni/PillSelect.svelte';
	import PillStepper from './omni/PillStepper.svelte';
	import PillPopover from './omni/PillPopover.svelte';
	import MediaTile from './omni/MediaTile.svelte';

	interface WorkflowOption {
		id: number;
		name: string;
		kind: string;
	}

	interface Props {
		inputs: ComfyDynamicInput[];
		values: Record<string, string | number>;
		/** Current workflow (for model pill label). */
		workflow?: WorkflowOption | null;
		/** Available workflows (for model pill dropdown). */
		workflows?: WorkflowOption[];
		onWorkflowChange?: (id: number) => void;
		assetOptions?: AssetOption[];
		allowUpload?: boolean;
		showErrors?: boolean;
		onValidityChange?: (missing: string[]) => void;
		onChange?: (values: Record<string, string | number>) => void;
		/** Enter or Ctrl+Enter in prompt → trigger Generate. */
		onSubmit?: () => void;
		/** Pending state for the Generate button (shows spinner-ish label). */
		submitting?: boolean;
	/** Disable the Generate button (e.g. workflow cannot fulfil the scene). */
	disabled?: boolean;
	/** Why Generate is disabled — surfaced as the button's title tooltip. */
	generateDisabledHint?: string;
		/** Custom label for the Generate button. */
		generateLabel?: string;
	}

	let {
		inputs,
		values = $bindable(),
		workflow = null,
		workflows = [],
		onWorkflowChange,
		assetOptions = [],
		allowUpload = false,
		showErrors = false,
		onValidityChange,
		onChange,
		onSubmit,
		submitting = false,
		disabled = false,
		generateDisabledHint = '',
		generateLabel = 'Generate',
	}: Props = $props();

	const uploadMgr = createUploadManager();

	// Classify inputs into UI zones
	const classified = $derived(classifyAll(inputs));

	// ── Value helpers ─────────────────────────────────────────────────────

	/** Prefill undefined fields from workflow JSON defaults (mirrors ComfyDynamicForm). */
	$effect(() => {
		const next = { ...values };
		let changed = false;
		for (const inp of inputs) {
			if (
				next[inp.nodeId] === undefined &&
				inp.defaultValue !== undefined &&
				inp.defaultValue !== ''
			) {
				next[inp.nodeId] = inp.defaultValue;
				changed = true;
			}
		}
		if (changed) {
			values = next;
			onChange?.(values);
		}
	});

	function setValue(nodeId: string, value: string | number) {
		values = { ...values, [nodeId]: value };
		onChange?.(values);
	}

	function clearValue(nodeId: string) {
		values = { ...values, [nodeId]: '' };
		onChange?.(values);
	}

	// ── Resolution pill ───────────────────────────────────────────────────

	const resPair = $derived(classified.resolutionPair);

	const currentResLabel = $derived(
		resPair
			? resolutionLabel(values[resPair.width.input.nodeId], values[resPair.height.input.nodeId])
			: null,
	);

	function setResolution(w: number, h: number) {
		if (!resPair) return;
		const next = { ...values };
		next[resPair.width.input.nodeId] = w;
		next[resPair.height.input.nodeId] = h;
		values = next;
		onChange?.(values);
	}

	const resOptions = $derived(
		RESOLUTION_PRESETS.map((p) => ({
			value: `${p.width}x${p.height}`,
			label: `${p.label} (${p.width}×${p.height})`,
		})),
	);

	const currentResValue = $derived.by(() => {
		if (!resPair) return null;
		const w = values[resPair.width.input.nodeId];
		const h = values[resPair.height.input.nodeId];
		return w && h ? `${w}x${h}` : null;
	});

	function onResChange(val: string | number) {
		const [w, h] = String(val).split('x').map(Number);
		if (w && h) setResolution(w, h);
	}

	// ── Workflow / model pill ─────────────────────────────────────────────

	const wfOptions = $derived(
		(workflows ?? []).map((w) => ({ value: w.id, label: w.name })),
	);

	function onWfChange(val: string | number) {
		onWorkflowChange?.(Number(val));
	}

	// ── Media tiles ───────────────────────────────────────────────────────

	async function handleFileUpload(nodeId: string, file: File) {
		const path = await uploadMgr.uploadSafe(nodeId, file);
		if (path) {
			setValue(nodeId, path);
		}
	}

	function handleAssetSelect(nodeId: string, path: string) {
		setValue(nodeId, path);
	}

	// ── Prompt textarea ───────────────────────────────────────────────────

	const promptNode = $derived(classified.prompt?.input ?? null);
	const negativeNode = $derived(classified.negative?.input ?? null);
	let showNegative = $state(false);

	function onPromptKeydown(e: KeyboardEvent) {
		if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
			e.preventDefault();
			onSubmit?.();
		}
	}

	// ── Validity tracking ─────────────────────────────────────────────────

	function isBlank(val: string | number | undefined): boolean {
		return val === undefined || (typeof val === 'string' && !val.trim());
	}

	const missingLabels = $derived(
		inputs.filter((inp) => inp.required && isBlank(values[inp.nodeId])).map((inp) => inp.label),
	);

	$effect(() => {
		onValidityChange?.(missingLabels);
	});

	// ── Advanced popover fields ───────────────────────────────────────────

	const hasAdvanced = $derived(classified.advanced.length > 0);
</script>

<div class="omni-shell">
	<!-- ── Composer body ─────────────────────────────────────────────── -->
	<div class="omni-composer" class:has-media={classified.media.length > 0}>
		{#if classified.media.length > 0}
			<div class="media-tray">
				{#each classified.media as mc (mc.input.nodeId)}
					{@const nodeId = mc.input.nodeId}
					<MediaTile
						input={mc.input}
						value={String(values[nodeId] ?? '')}
						uploadingName={uploadMgr.uploading[nodeId] ?? null}
						{assetOptions}
						{allowUpload}
						invalid={showErrors && isBlank(values[nodeId])}
						onselectFile={(file) => handleFileUpload(nodeId, file)}
						onselectAsset={(path) => handleAssetSelect(nodeId, path)}
						onclear={() => clearValue(nodeId)}
					/>
				{/each}
			</div>
		{/if}

		{#if promptNode}
			<textarea
				class="prompt-area"
				placeholder="Describe the scene you want to generate…"
				rows="3"
				value={values[promptNode.nodeId] ?? ''}
				oninput={(e) => setValue(promptNode.nodeId, e.currentTarget.value)}
				onkeydown={onPromptKeydown}
				aria-label="Prompt"
			></textarea>
		{:else}
			<textarea
				class="prompt-area no-prompt-role"
				placeholder="This workflow has no (Input:prompt) field. Use the Advanced pill below for raw inputs."
				rows="2"
				disabled
			></textarea>
		{/if}

		{#if negativeNode}
			<div class="negative-section">
				{#if showNegative}
					<textarea
						class="negative-area"
						placeholder="Negative prompt (what to avoid)…"
						rows="2"
						value={values[negativeNode.nodeId] ?? ''}
						oninput={(e) => setValue(negativeNode.nodeId, e.currentTarget.value)}
						onkeydown={onPromptKeydown}
						aria-label="Negative prompt"
					></textarea>
				{/if}
				<button type="button" class="negative-toggle" onclick={() => (showNegative = !showNegative)}>
					<Icon name={showNegative ? 'chevron-up' : 'chevron-down'} size={12} />
					Negative prompt
				</button>
			</div>
		{/if}
	</div>

	<!-- ── Control bar ───────────────────────────────────────────────── -->
	<div class="omni-controls">
		<!-- Model / workflow selector -->
		{#if workflow && workflows.length > 0}
			<PillSelect
				label={workflow.name}
				options={wfOptions}
				value={workflow.id}
				onchange={onWfChange}
				icon="sparkle"
				highlight
			/>
		{/if}

		<!-- Resolution pill (merged width + height) -->
		{#if resPair}
			<PillSelect
				label={currentResLabel ?? 'Resolution'}
				options={resOptions}
				value={currentResValue}
				onchange={onResChange}
				icon="image"
			/>
		{/if}

		<!-- Standalone width/height (only if not a pair) -->
		{#each classified.control.filter((c) => c.widget === 'resolutionPill') as ctrl (ctrl.input.nodeId)}
			{@const nodeId = ctrl.input.nodeId}
			<PillStepper
				label={ctrl.input.label}
				value={Number(values[nodeId]) || 0}
				min={256}
				max={4096}
				step={64}
				onchange={(v) => setValue(nodeId, v)}
			/>
		{/each}

		<!-- Duration -->
		{#each classified.control.filter((c) => normalizeInputRole(c.input.role) === 'duration') as ctrl (ctrl.input.nodeId)}
			{@const nodeId = ctrl.input.nodeId}
			<PillStepper
				label="Duration"
				value={values[nodeId] ?? ctrl.input.defaultValue ?? 5}
				min={1}
				max={30}
				step={1}
				onchange={(v) => setValue(nodeId, v)}
			/>
		{/each}

		<!-- Seed -->
		{#each classified.control.filter((c) => normalizeInputRole(c.input.role) === 'seed') as ctrl (ctrl.input.nodeId)}
			{@const nodeId = ctrl.input.nodeId}
			<PillStepper
				label="Seed"
				value={values[nodeId] ?? ctrl.input.defaultValue ?? 0}
				min={0}
				max={999999999}
				step={1}
				onchange={(v) => setValue(nodeId, v)}
			/>
		{/each}

		<!-- Advanced (unknown roles, extra params) -->
		{#if hasAdvanced}
			<PillPopover label="Advanced" badge={classified.advanced.length} icon="settings">
				{#each classified.advanced as ctrl (ctrl.input.nodeId)}
					{@const nodeId = ctrl.input.nodeId}
					<label class="adv-field">
						<span class="adv-label">{ctrl.input.label}</span>
						{#if ctrl.input.kind === 'number'}
							<input
								class="adv-input"
								type="number"
								value={values[nodeId] ?? ctrl.input.defaultValue ?? ''}
								oninput={(e) => setValue(nodeId, Number(e.currentTarget.value) || 0)}
							/>
						{:else}
							<input
								class="adv-input"
								type="text"
								value={values[nodeId] ?? ctrl.input.defaultValue ?? ''}
								oninput={(e) => setValue(nodeId, e.currentTarget.value)}
							/>
						{/if}
					</label>
				{/each}
			</PillPopover>
		{/if}

		<!-- Generate -->
		<button
			type="button"
			class="generate-btn"
			class:disabled={submitting || disabled}
			disabled={submitting || disabled}
			title={disabled ? generateDisabledHint : undefined}
			onclick={() => onSubmit?.()}
			aria-label="Generate"
		>
			<Icon name="sparkle" size={16} />
			{submitting ? 'Queuing…' : generateLabel}
		</button>
	</div>
</div>

<style>
	.omni-shell {
		display: flex;
		flex-direction: column;
		background: var(--bg-surface);
		border: 1px solid var(--border);
		border-radius: var(--radius-lg);
		overflow: hidden;
		/* Prevent flex parents from collapsing this via overflow+min-height:auto */
		flex-shrink: 0;
		min-height: fit-content;
	}

	/* ── Composer ──────────────────────────────────────────── */
	.omni-composer {
		display: flex;
		flex-direction: column;
		gap: 0;
		padding: 16px;
		min-height: 120px;
	}

	.media-tray {
		display: flex;
		gap: 10px;
		flex-wrap: wrap;
		margin-bottom: 12px;
	}

	.prompt-area {
		width: 100%;
		box-sizing: border-box;
		background: transparent;
		border: none;
		color: var(--text-primary);
		font-family: var(--font-body);
		font-size: 15px;
		line-height: 1.6;
		resize: vertical;
		min-height: 60px;
		outline: none;
		padding: 0;
	}

	.prompt-area::placeholder {
		color: var(--text-muted);
	}

	.prompt-area:focus-visible {
		outline: none;
	}

	.prompt-area.no-prompt-role {
		color: var(--text-muted);
		font-style: italic;
		cursor: not-allowed;
	}

	.negative-section {
		margin-top: 8px;
		border-top: 1px solid var(--border);
		padding-top: 8px;
	}

	.negative-area {
		width: 100%;
		box-sizing: border-box;
		background: transparent;
		border: none;
		color: var(--text-secondary);
		font-family: var(--font-body);
		font-size: 13px;
		line-height: 1.5;
		resize: vertical;
		min-height: 40px;
		outline: none;
		padding: 0;
		margin-bottom: 4px;
	}

	.negative-area::placeholder {
		color: var(--text-muted);
	}

	.negative-toggle {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		background: transparent;
		border: none;
		color: var(--text-muted);
		font-size: 12px;
		font-family: var(--font-body);
		cursor: pointer;
		padding: 2px 0;
	}

	.negative-toggle:hover {
		color: var(--text-secondary);
	}

	/* ── Control bar ───────────────────────────────────────── */
	.omni-controls {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 10px 16px;
		border-top: 1px solid var(--border);
		background: var(--bg-primary);
		flex-wrap: wrap;
	}

	.generate-btn {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		height: 36px;
		padding: 0 20px;
		border-radius: 9999px;
		background: var(--success, #22c55e);
		border: none;
		color: #000;
		font-size: 14px;
		font-weight: 700;
		font-family: var(--font-body);
		cursor: pointer;
		margin-left: auto;
		transition: filter 0.15s, transform 0.1s;
	}

	.generate-btn:hover {
		filter: brightness(1.1);
	}

	.generate-btn:active {
		transform: scale(0.97);
	}

	.generate-btn.disabled,
	.generate-btn:disabled {
		cursor: default;
		opacity: 0.7;
		transform: none;
		filter: none;
	}

	.generate-btn:focus-visible {
		outline: none;
		box-shadow: 0 0 0 3px color-mix(in srgb, var(--success) 35%, transparent);
	}

	/* ── Advanced popover fields ───────────────────────────── */
	.adv-field {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.adv-label {
		font-size: 12px;
		font-weight: 600;
		color: var(--text-secondary);
	}

	.adv-input {
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 8px 10px;
		color: var(--text-primary);
		font-size: 13px;
		font-family: var(--font-mono);
		width: 100%;
		box-sizing: border-box;
	}

	.adv-input:focus-visible {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 3px var(--accent-glow);
	}
</style>
