<script lang="ts">
	import { assetUrl, playgroundApi, type UploadKind } from '$lib/api';
	import type { ComfyDynamicInput } from '$lib/comfy/types';
	import { toast } from '$lib/toast';
	import Icon from './ui/Icon.svelte';
	import Spinner from './ui/Spinner.svelte';

	interface Props {
		inputs: ComfyDynamicInput[];
		values?: Record<string, string | number>;
		readonly?: boolean;
		/** Hide technical role/kind chips (Playground human labels). */
		quiet?: boolean;
		assetOptions?: Array<{ label: string; path: string }>;
		/** Allow client-file uploads (dropzone) for image/audio inputs. */
		allowUpload?: boolean;
		/** Force required-field error rings visible (e.g. after a blocked Generate). */
		showErrors?: boolean;
		/** Emits labels of required-but-empty inputs whenever the set changes. */
		onValidityChange?: (missing: string[]) => void;
		onChange?: (values: Record<string, string | number>) => void;
	}

	let {
		inputs,
		values = $bindable({}),
		readonly = false,
		quiet = false,
		assetOptions = [],
		allowUpload = false,
		showErrors = false,
		onValidityChange,
		onChange,
	}: Props = $props();

	// Unique per mounted form so label/id pairs never collide across instances.
	const uid = Math.random().toString(36).slice(2, 8);
	let touched = $state<Record<string, boolean>>({});

	function inputId(nodeId: string): string {
		return `cdf-${uid}-${nodeId.replace(/[^a-zA-Z0-9_-]/g, '')}`;
	}

	function setValue(nodeId: string, value: string | number) {
		values = { ...values, [nodeId]: value };
		onChange?.(values);
	}

	function setNumberValue(nodeId: string, raw: string) {
		// '' must stay '' — Number('') === 0 would silently turn a cleared field into 0.
		// Blank strings are stripped by compactInputValues before submission.
		if (raw === '') {
			setValue(nodeId, '');
			return;
		}
		const n = Number(raw);
		if (!Number.isNaN(n)) setValue(nodeId, n);
	}

	function markTouched(nodeId: string) {
		if (!touched[nodeId]) touched = { ...touched, [nodeId]: true };
	}

	function isBlank(value: string | number | undefined): boolean {
		return value === undefined || (typeof value === 'string' && !value.trim());
	}

	function isMissing(inp: ComfyDynamicInput): boolean {
		return inp.required && isBlank(values[inp.nodeId]);
	}

	const missingLabels = $derived(inputs.filter(isMissing).map((inp) => inp.label));

	$effect(() => {
		onValidityChange?.(missingLabels);
	});

	function showError(inp: ComfyDynamicInput): boolean {
		return isMissing(inp) && (showErrors || !!touched[inp.nodeId]);
	}

	function thumbFor(current: string): string | null {
		if (!current) return null;
		const opt = assetOptions.find((o) => o.path === current);
		return opt ? assetUrl(opt.path) : null;
	}

	/** Hide a broken preview image (asset moved/deleted on disk). */
	function hideBrokenThumb(e: Event) {
		const img = e.currentTarget as HTMLImageElement | null;
		if (img) img.style.display = 'none';
	}

	function isImageKind(kind: string) {
		return kind === 'image' || kind === 'image_url';
	}

	function isUploadKind(kind: string) {
		return isImageKind(kind) || kind === 'audio' || kind === 'video';
	}

	// --- Client-file uploads (allowUpload) ---
	// One shared hidden file input serves every uploadable field; uploadNodeId
	// remembers which field the picker was opened for.
	let fileInput = $state<HTMLInputElement | null>(null);
	let uploadNodeId: string | null = null;
	let uploading = $state<Record<string, string>>({});
	let dragDepth = $state<Record<string, number>>({});
	let uploadedMeta = $state<Record<string, { name: string; kind: UploadKind }>>({});

	function uploadKindLabel(kind: string) {
		return kind === 'audio' ? 'audio' : kind === 'video' ? 'video' : 'image';
	}

	function acceptFor(kind: string) {
		if (kind === 'audio') return 'audio/*';
		if (kind === 'video') return 'video/*,.mp4,.webm,.mov,.mkv';
		return 'image/*';
	}

	function truncateMiddle(name: string, max = 32): string {
		if (name.length <= max) return name;
		const head = Math.ceil((max - 1) / 2);
		const tail = Math.floor((max - 1) / 2);
		return `${name.slice(0, head)}…${name.slice(name.length - tail)}`;
	}

	/** Display name + media kind for an uploaded path (falls back to extension sniffing). */
	function uploadMetaFor(path: string): { name: string; kind: UploadKind } {
		const meta = uploadedMeta[path];
		if (meta) return meta;
		const base = path.split(/[/\\]/).pop() ?? path;
		const ext = base.slice(base.lastIndexOf('.')).toLowerCase();
		if (['.mp4', '.webm', '.mov', '.mkv'].includes(ext)) return { name: base, kind: 'video' };
		if (['.mp3', '.wav', '.flac', '.ogg', '.m4a'].includes(ext))
			return { name: base, kind: 'audio' };
		return { name: base, kind: 'image' };
	}

	function openFilePicker(inp: ComfyDynamicInput) {
		if (!fileInput || uploading[inp.nodeId]) return;
		uploadNodeId = inp.nodeId;
		fileInput.accept = acceptFor(inp.kind);
		fileInput.value = '';
		fileInput.click();
	}

	function onFileChosen(e: Event) {
		const el = e.currentTarget as HTMLInputElement;
		const file = el.files?.[0];
		const nodeId = uploadNodeId;
		uploadNodeId = null;
		if (!file || !nodeId) return;
		const inp = inputs.find((i) => i.nodeId === nodeId);
		if (inp) void doUpload(inp, file);
	}

	function onUploadDragEnter(nodeId: string, e: DragEvent) {
		e.preventDefault();
		dragDepth = { ...dragDepth, [nodeId]: (dragDepth[nodeId] ?? 0) + 1 };
	}

	function onUploadDragLeave(nodeId: string) {
		dragDepth = { ...dragDepth, [nodeId]: Math.max(0, (dragDepth[nodeId] ?? 0) - 1) };
	}

	function onUploadDrop(inp: ComfyDynamicInput, e: DragEvent) {
		e.preventDefault();
		dragDepth = { ...dragDepth, [inp.nodeId]: 0 };
		if (uploading[inp.nodeId]) return;
		const file = e.dataTransfer?.files?.[0];
		if (file) void doUpload(inp, file);
	}

	async function doUpload(inp: ComfyDynamicInput, file: File) {
		uploading = { ...uploading, [inp.nodeId]: file.name };
		try {
			const res = await playgroundApi.upload(file);
			uploadedMeta = { ...uploadedMeta, [res.path]: { name: res.name, kind: res.kind } };
			setValue(inp.nodeId, res.path);
			markTouched(inp.nodeId);
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Upload failed');
		} finally {
			const next = { ...uploading };
			delete next[inp.nodeId];
			uploading = next;
		}
	}

	$effect(() => {
		const next = { ...values };
		let changed = false;
		for (const inp of inputs) {
			if (next[inp.nodeId] === undefined && inp.defaultValue !== undefined && inp.defaultValue !== '') {
				next[inp.nodeId] = inp.defaultValue;
				changed = true;
			}
		}
		if (changed) {
			values = next;
			onChange?.(values);
		}
	});
</script>

{#if inputs.length === 0}
	<p class="muted">
		No (Input) / (Input:role) nodes detected. In ComfyUI, title editable nodes like
		<code>(Input:prompt)</code> — see AGENTS.md.
	</p>
{:else}
	<div class="form">
		{#if allowUpload}
			<input
				bind:this={fileInput}
				type="file"
				class="sr-only-file"
				tabindex="-1"
				aria-hidden="true"
				onchange={onFileChosen}
			/>
		{/if}
		{#each inputs as inp (inp.nodeId)}
			{@const current = String(values[inp.nodeId] ?? '')}
			{@const id = inputId(inp.nodeId)}
			{@const errored = showError(inp)}
			<div class="field">
				<label class="label" for={id}>
					{inp.label}
					{#if inp.required}
						<span class="req" title="Required" aria-hidden="true">*</span>
					{/if}
					{#if !quiet}
						{#if inp.role}<span class="role">{inp.role}</span>{/if}
						<span class="kind">{inp.kind}</span>
					{/if}
				</label>
				{#if readonly}
					<div class="readonly">{values[inp.nodeId] ?? inp.defaultValue ?? '—'}</div>
				{:else if inp.kind === 'textarea'}
					<textarea
						{id}
						rows="3"
						class:invalid={errored}
						aria-required={inp.required || undefined}
						aria-invalid={errored || undefined}
						value={current}
						oninput={(e) => setValue(inp.nodeId, e.currentTarget.value)}
						onblur={() => markTouched(inp.nodeId)}
					></textarea>
				{:else if inp.kind === 'number'}
					<input
						{id}
						type="number"
						class:invalid={errored}
						aria-required={inp.required || undefined}
						aria-invalid={errored || undefined}
						value={values[inp.nodeId] ?? ''}
						oninput={(e) => setNumberValue(inp.nodeId, e.currentTarget.value)}
						onblur={() => markTouched(inp.nodeId)}
					/>
				{:else if allowUpload && isUploadKind(inp.kind)}
					{@const uploadingName = uploading[inp.nodeId]}
					{@const isUpload = !!current && !assetOptions.some((o) => o.path === current)}
					<div class="upload-field">
						{#if isUpload}
							{@const meta = uploadMetaFor(current)}
							<div class="upload-preview">
								{#if meta.kind === 'image'}
									<img
										class="upload-thumb"
										src={assetUrl(current)}
										alt="Uploaded file preview"
										onerror={hideBrokenThumb}
									/>
								{:else}
									<span class="upload-file-icon" aria-hidden="true">
										<Icon name={meta.kind === 'audio' ? 'music' : 'film'} size={18} />
									</span>
								{/if}
								<span class="upload-name" title={meta.name}>{truncateMiddle(meta.name)}</span>
								<button
									type="button"
									class="upload-clear"
									aria-label="Clear"
									onclick={() => setValue(inp.nodeId, '')}
								>
									<Icon name="close" size={14} />
								</button>
							</div>
						{/if}
						<button
							type="button"
							id={assetOptions.length ? `${id}-upload` : id}
							class="dropzone"
							class:drag={(dragDepth[inp.nodeId] ?? 0) > 0}
							class:invalid={errored}
							disabled={!!uploadingName}
							aria-busy={!!uploadingName || undefined}
							onclick={() => openFilePicker(inp)}
							ondragenter={(e) => onUploadDragEnter(inp.nodeId, e)}
							ondragleave={() => onUploadDragLeave(inp.nodeId)}
							ondragover={(e) => e.preventDefault()}
							ondrop={(e) => onUploadDrop(inp, e)}
						>
							{#if uploadingName}
								<Spinner size="sm" />
								<span>Uploading {truncateMiddle(uploadingName, 24)}…</span>
							{:else}
								<Icon name="upload" size={16} />
								<span>Drop {uploadKindLabel(inp.kind)} here or <u>browse</u></span>
							{/if}
						</button>
						{#if assetOptions.length}
							{@const thumb = thumbFor(current)}
							<p class="upload-divider" aria-hidden="true">
								<span>or choose an existing asset</span>
							</p>
							<div class="pick-row">
								{#if isImageKind(inp.kind) && thumb}
									<img
										class="pick-thumb"
										src={thumb}
										alt="Selected asset preview"
										onerror={hideBrokenThumb}
									/>
								{/if}
								<select
									{id}
									class="pick-select"
									class:invalid={errored}
									aria-required={inp.required || undefined}
									aria-invalid={errored || undefined}
									value={current}
									onchange={(e) => {
										setValue(inp.nodeId, e.currentTarget.value);
										markTouched(inp.nodeId);
									}}
								>
									<option value="">Choose asset…</option>
									{#each assetOptions as opt}
										<option value={opt.path}>{opt.label}</option>
									{/each}
								</select>
								{#if current}
									<button
										type="button"
										class="clear"
										onclick={() => setValue(inp.nodeId, '')}
									>
										Clear
									</button>
								{/if}
							</div>
						{/if}
					</div>
				{:else if isImageKind(inp.kind)}
					{#if assetOptions.length}
						{@const thumb = thumbFor(current)}
						<div class="pick-row">
							{#if thumb}
								<img
									class="pick-thumb"
									src={thumb}
									alt="Selected asset preview"
									onerror={hideBrokenThumb}
								/>
							{/if}
							<select
								{id}
								class="pick-select"
								class:invalid={errored}
								aria-required={inp.required || undefined}
								aria-invalid={errored || undefined}
								value={current}
								onchange={(e) => {
									setValue(inp.nodeId, e.currentTarget.value);
									markTouched(inp.nodeId);
								}}
							>
								<option value="">Choose asset…</option>
								{#each assetOptions as opt}
									<option value={opt.path}>{opt.label}</option>
								{/each}
							</select>
							{#if current}
								<button
									type="button"
									class="clear"
									onclick={() => setValue(inp.nodeId, '')}
								>
									Clear
								</button>
							{/if}
						</div>
					{:else}
						<p class="hint">No assets yet — generate sheets / environments in Assets first.</p>
					{/if}
				{:else if inp.kind === 'audio'}
					{#if assetOptions.length}
						<select
							{id}
							class:invalid={errored}
							aria-required={inp.required || undefined}
							aria-invalid={errored || undefined}
							value={current}
							onchange={(e) => {
								setValue(inp.nodeId, e.currentTarget.value);
								markTouched(inp.nodeId);
							}}
						>
							<option value="">Choose asset…</option>
							{#each assetOptions as opt}
								<option value={opt.path}>{opt.label}</option>
							{/each}
						</select>
					{:else}
						<input
							{id}
							type="text"
							class:invalid={errored}
							aria-required={inp.required || undefined}
							aria-invalid={errored || undefined}
							placeholder="Local path"
							value={current}
							oninput={(e) => setValue(inp.nodeId, e.currentTarget.value)}
							onblur={() => markTouched(inp.nodeId)}
						/>
					{/if}
				{:else}
					<input
						{id}
						type="text"
						class:invalid={errored}
						aria-required={inp.required || undefined}
						aria-invalid={errored || undefined}
						value={current}
						oninput={(e) => setValue(inp.nodeId, e.currentTarget.value)}
						onblur={() => markTouched(inp.nodeId)}
					/>
				{/if}
				{#if errored}
					<span class="error-msg" role="alert">{inp.label} is required</span>
				{/if}
			</div>
		{/each}
	</div>
{/if}

<style>
	.form {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}
	.field {
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 13px;
		color: var(--text-secondary);
	}
	.label {
		font-weight: 600;
		color: var(--text-primary);
		font-size: 12px;
	}
	.req {
		color: var(--error);
		margin-left: 2px;
		font-weight: 700;
	}
	.kind {
		font-weight: 400;
		color: var(--text-muted);
		font-size: 11px;
		margin-left: 6px;
	}
	.role {
		font-weight: 600;
		color: var(--accent, #a78bfa);
		font-size: 11px;
		margin-left: 6px;
		font-family: ui-monospace, monospace;
	}
	.role::before {
		content: 'role:';
	}
	input,
	textarea,
	select {
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 8px 10px;
		color: var(--text-primary);
		font-size: 13px;
	}
	input:focus-visible,
	textarea:focus-visible,
	select:focus-visible {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 3px var(--accent-glow);
	}
	input.invalid,
	textarea.invalid,
	select.invalid {
		border-color: var(--error);
	}
	input.invalid:focus-visible,
	textarea.invalid:focus-visible,
	select.invalid:focus-visible {
		box-shadow: 0 0 0 3px rgba(239, 68, 68, 0.25);
	}
	.error-msg {
		font-size: 11px;
		color: var(--error);
	}
	.pick-row {
		display: flex;
		gap: 8px;
		align-items: center;
		min-width: 0;
	}
	.pick-thumb {
		width: 36px;
		height: 36px;
		flex-shrink: 0;
		border-radius: var(--radius-sm);
		border: 1px solid var(--border);
		object-fit: cover;
		background: var(--bg-elevated);
	}
	.pick-select {
		flex: 1;
		min-width: 0;
		min-height: 36px;
	}
	.clear {
		flex-shrink: 0;
		border: 1px solid var(--border);
		background: transparent;
		color: var(--text-muted);
		font-size: 12px;
		font-weight: 600;
		cursor: pointer;
		padding: 0 10px;
		border-radius: var(--radius-sm);
		min-height: 36px;
	}
	.clear:hover {
		color: var(--error);
		border-color: rgba(239, 68, 68, 0.4);
	}
	.clear:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.readonly {
		padding: 8px 0;
		color: var(--text-secondary);
		word-break: break-all;
	}
	.muted {
		color: var(--text-muted);
		font-size: 13px;
	}
	.hint {
		margin: 0;
		font-size: 12px;
		color: var(--text-muted);
	}
	.sr-only-file {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0 0 0 0);
		white-space: nowrap;
		border: 0;
	}
	.upload-field {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.dropzone {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 8px;
		min-height: 64px;
		padding: 10px 12px;
		border: 1.5px dashed var(--border);
		border-radius: var(--radius-sm);
		background: var(--bg-elevated);
		color: var(--text-secondary);
		font-size: 13px;
		cursor: pointer;
		transition:
			border-color 0.15s,
			background 0.15s;
	}
	.dropzone:hover:not(:disabled) {
		border-color: var(--text-muted);
	}
	.dropzone:focus-visible {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 3px var(--accent-glow);
	}
	.dropzone.drag {
		border-color: var(--accent);
		background: var(--accent-glow);
	}
	.dropzone:disabled {
		cursor: default;
		opacity: 0.7;
	}
	.dropzone.invalid {
		border-color: var(--error);
	}
	.upload-divider {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 0;
		font-size: 11px;
		color: var(--text-muted);
	}
	.upload-divider::before,
	.upload-divider::after {
		content: '';
		flex: 1;
		border-top: 1px solid var(--border);
	}
	.upload-preview {
		display: flex;
		align-items: center;
		gap: 8px;
		min-width: 0;
	}
	.upload-thumb {
		width: 48px;
		height: 48px;
		flex-shrink: 0;
		border-radius: var(--radius-sm);
		border: 1px solid var(--border);
		object-fit: cover;
		background: var(--bg-elevated);
	}
	.upload-file-icon {
		width: 48px;
		height: 48px;
		flex-shrink: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		border-radius: var(--radius-sm);
		border: 1px solid var(--border);
		background: var(--bg-elevated);
		color: var(--text-secondary);
	}
	.upload-name {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--text-primary);
	}
	.upload-clear {
		flex-shrink: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 26px;
		height: 26px;
		border: none;
		border-radius: var(--radius-sm);
		background: transparent;
		color: var(--text-muted);
		cursor: pointer;
	}
	.upload-clear:hover {
		color: var(--error);
	}
	.upload-clear:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
</style>
