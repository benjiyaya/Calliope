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
	import { t } from '$lib/i18n.svelte';
	import PillSelect from './omni/PillSelect.svelte';
	import PillStepper from './omni/PillStepper.svelte';
	import MediaTile from './omni/MediaTile.svelte';

	interface WorkflowOption {
		id: number;
		name: string;
		kind: string;
	}

	interface Props {
		inputs: ComfyDynamicInput[];
		values: Record<string, string | number | boolean>;
		/** Current workflow (for model pill label). */
		workflow?: WorkflowOption | null;
		/** Available workflows (for model pill dropdown). */
		workflows?: WorkflowOption[];
		onWorkflowChange?: (id: number) => void;
		assetOptions?: AssetOption[];
		allowUpload?: boolean;
		showErrors?: boolean;
		onValidityChange?: (missing: string[]) => void;
		onChange?: (values: Record<string, string | number | boolean>) => void;
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
		generateLabel = '',
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

	function setValue(nodeId: string, value: string | number | boolean) {
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

	function isBlank(val: string | number | boolean | undefined): boolean {
		if (typeof val === 'boolean') return false;
		return val === undefined || (typeof val === 'string' && !val.trim());
	}

	function boolChecked(value: string | number | boolean | undefined): boolean {
		return value === true || value === 'true' || value === 1 || value === '1';
	}

	const missingLabels = $derived(
		inputs.filter((inp) => inp.required && isBlank(values[inp.nodeId])).map((inp) => inp.label),
	);

	$effect(() => {
		onValidityChange?.(missingLabels);
	});
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
				placeholder={t('omni.promptPlaceholder')}
				rows="12"
				value={values[promptNode.nodeId] ?? ''}
				oninput={(e) => setValue(promptNode.nodeId, e.currentTarget.value)}
				onkeydown={onPromptKeydown}
				aria-label={t('omni.promptAria')}
			></textarea>
		{:else}
			<textarea
				class="prompt-area no-prompt-role"
				placeholder={t('omni.noPromptRole')}
				rows="2"
				disabled
			></textarea>
		{/if}

		{#if negativeNode}
			<div class="negative-section">
				{#if showNegative}
					<textarea
						class="negative-area"
						placeholder={t('omni.negativePlaceholder')}
						rows="2"
						value={values[negativeNode.nodeId] ?? ''}
						oninput={(e) => setValue(negativeNode.nodeId, e.currentTarget.value)}
						onkeydown={onPromptKeydown}
						aria-label={t('omni.negativeAria')}
					></textarea>
				{/if}
				<button type="button" class="negative-toggle" onclick={() => (showNegative = !showNegative)}>
					<Icon name={showNegative ? 'chevron-up' : 'chevron-down'} size={12} />
					{t('omni.negativeToggle')}
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
				label={currentResLabel ?? t('omni.resolution')}
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
				label={t('omni.duration')}
				value={values[nodeId] ?? ctrl.input.defaultValue ?? 5}
				min={1}
				max={30}
				step={1}
				unit="s"
				onchange={(v) => setValue(nodeId, v)}
			/>
		{/each}

		<!-- Seed -->
		{#each classified.control.filter((c) => normalizeInputRole(c.input.role) === 'seed') as ctrl (ctrl.input.nodeId)}
			{@const nodeId = ctrl.input.nodeId}
			<PillStepper
				label={t('omni.seed')}
				value={values[nodeId] ?? ctrl.input.defaultValue ?? 0}
				min={0}
				max={999999999}
				step={1}
				onchange={(v) => setValue(nodeId, v)}
			/>
		{/each}

		<!-- Advanced workflow options — rendered inline in the options row,
		     at the same level as the workflow selector, not hidden in a popover -->
		{#each classified.advanced as ctrl (ctrl.input.nodeId)}
			{@const nodeId = ctrl.input.nodeId}
			<label class="adv-inline" title={ctrl.input.label}>
				<span class="adv-inline-label">{ctrl.input.label}</span>
				{#if ctrl.input.kind === 'boolean'}
					<input
						class="adv-inline-check"
						type="checkbox"
						checked={boolChecked(values[nodeId] ?? ctrl.input.defaultValue)}
						onchange={(e) => setValue(nodeId, e.currentTarget.checked)}
					/>
				{:else}
					<input
						class="adv-inline-input"
						type={ctrl.input.kind === 'number' ? 'number' : 'text'}
						value={values[nodeId] ?? ctrl.input.defaultValue ?? ''}
						oninput={(e) =>
							setValue(
								nodeId,
								ctrl.input.kind === 'number'
									? Number(e.currentTarget.value) || 0
									: e.currentTarget.value,
							)}
					/>
				{/if}
			</label>
		{/each}
	</div>

	<!-- ── Generate row ───────────────────────────────────────────────── -->
	<div class="generate-row">
		<button
			type="button"
			class="generate-btn"
			class:disabled={submitting || disabled}
			disabled={submitting || disabled}
			title={disabled ? generateDisabledHint : undefined}
			onclick={() => onSubmit?.()}
			aria-label={t('omni.generateAria')}
		>
			<Icon name="sparkle" size={16} />
			{submitting ? t('omni.queuing') : generateLabel || t('omni.generate')}
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
		/* Never let a constrained ancestor squeeze the prompt box away. */
		flex-shrink: 0;
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
		/* Generous default — video prompts run long. Drag the handle for more. */
		min-height: 280px;
		height: 280px;
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
		flex-shrink: 0;
	}

	/* Let pills shrink + ellipsis so workflow / duration / advanced share the
	   same row even in the narrow inspector dock. */
	.omni-controls :global(.pill-wrap) {
		min-width: 0;
		flex: 0 1 auto;
	}

	/* Generate owns its own full-width row under the option pills.
	   NO flex-basis here — as a column-flex child, `100%` would resolve
	   against the shell's HEIGHT and starve the prompt box. */
	.generate-row {
		display: flex;
		justify-content: center;
		padding: 4px 0 12px;
		background: var(--bg-primary);
		flex-shrink: 0;
	}

	.generate-btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		width: 90%;
		height: 38px;
		padding: 0 20px;
		border-radius: 9999px;
		background: var(--success, #22c55e);
		border: none;
		color: #000;
		font-size: 14px;
		font-weight: 700;
		font-family: var(--font-body);
		cursor: pointer;
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

	/* ── Advanced options, inline in the options row ───────── */
	.adv-inline {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		height: var(--pill-height, 36px);
		padding: 0 12px;
		border-radius: var(--pill-radius, 9999px);
		background: var(--pill-bg, var(--bg-elevated));
		border: 1px solid var(--pill-border, var(--border));
		transition:
			border-color 0.15s,
			background 0.15s;
	}

	.adv-inline:focus-within {
		border-color: var(--accent);
		box-shadow: 0 0 0 3px var(--accent-glow);
	}

	.adv-inline-label {
		font-size: 12px;
		font-weight: 600;
		color: var(--text-secondary);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		max-width: 110px;
	}

	.adv-inline-check {
		width: 16px;
		height: 16px;
		margin: 0;
		accent-color: var(--accent);
	}

	.adv-inline-input {
		width: 64px;
		min-width: 0;
		background: transparent;
		border: none;
		outline: none;
		color: var(--text-primary);
		font-size: 13px;
		font-family: var(--font-mono);
		text-align: right;
	}

	.adv-inline-input::-webkit-outer-spin-button,
	.adv-inline-input::-webkit-inner-spin-button {
		-webkit-appearance: none;
		margin: 0;
	}

	.adv-inline-input[type='number'] {
		-moz-appearance: textfield;
		appearance: textfield;
	}
</style>
