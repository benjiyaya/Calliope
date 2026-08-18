<script lang="ts">
	import { createQuery, useQueryClient } from '@tanstack/svelte-query';
	import { workflows, type Workflow, type ComfyDynamicInput, type ComfyDynamicOutput } from '$lib/api';
	import { toast } from '$lib/toast';
	import Button from '$lib/components/ui/Button.svelte';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import Skeleton from '$lib/components/ui/Skeleton.svelte';
	import StatusChip from '$lib/components/ui/StatusChip.svelte';

	const client = useQueryClient();
	const workflowsQuery = createQuery({
		queryKey: ['workflows'],
		queryFn: workflows.list,
	});

	let jsonText = $state('');
	let uploadedFileName = $state<string | null>(null);
	let dragging = $state(false);
	let parseError = $state('');
	let analyzedInputs = $state<ComfyDynamicInput[]>([]);
	let analyzedOutputs = $state<ComfyDynamicOutput[]>([]);
	let analyzed = $state(false);
	let pendingJson = $state<Record<string, unknown> | null>(null);

	let wfName = $state('');
	let wfKind = $state<'image' | 'video'>('image');
	let wfProfile = $state('prose');
	let wfDescription = $state('');
	let saving = $state(false);

	let editingId = $state<number | null>(null);
	let editName = $state('');
	let editDescription = $state('');
	let editProfile = $state('prose');
	let detailsId = $state<number | null>(null);

	async function analyzeRaw(text: string) {
		parseError = '';
		analyzed = false;
		analyzedInputs = [];
		analyzedOutputs = [];
		pendingJson = null;
		try {
			const json = JSON.parse(text) as Record<string, unknown>;
			pendingJson = json;
			const result = await workflows.analyze(json);
			analyzedInputs = result.inputs;
			analyzedOutputs = result.outputs;
			analyzed = true;
			wfProfile = result.suggested_profile ?? 'prose';
			if (!wfName.trim() && uploadedFileName) {
				wfName = uploadedFileName.replace(/\.json$/i, '');
			}
		} catch (err) {
			parseError = err instanceof Error ? err.message : 'Invalid JSON';
		}
	}

	async function onFile(file: File) {
		uploadedFileName = file.name;
		const text = await file.text();
		jsonText = text;
		await analyzeRaw(text);
	}

	function onDrop(e: DragEvent) {
		e.preventDefault();
		dragging = false;
		const file = e.dataTransfer?.files?.[0];
		if (file) void onFile(file);
	}

	async function saveToLibrary() {
		if (!pendingJson || !wfName.trim() || !analyzed) return;
		saving = true;
		try {
			await workflows.create({
				name: wfName.trim(),
				kind: wfKind,
				workflow_json: pendingJson,
				description: wfDescription.trim() || undefined,
				prompt_profile: wfProfile,
			});
			jsonText = '';
			uploadedFileName = null;
			analyzed = false;
			analyzedInputs = [];
			analyzedOutputs = [];
			pendingJson = null;
			const name = wfName.trim();
			wfName = '';
			wfDescription = '';
			wfKind = 'image';
			wfProfile = 'prose';
			client.invalidateQueries({ queryKey: ['workflows'] });
			toast.success(`Workflow “${name}” saved to library`);
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Could not save workflow');
		} finally {
			saving = false;
		}
	}

	function startEdit(wf: Workflow) {
		editingId = wf.id;
		editName = wf.name;
		editDescription = wf.description ?? '';
		editProfile = wf.prompt_profile ?? 'prose';
	}

	async function saveEdit(id: number) {
		try {
			await workflows.update(id, {
				name: editName.trim(),
				description: editDescription.trim(),
				prompt_profile: editProfile,
			});
			editingId = null;
			client.invalidateQueries({ queryKey: ['workflows'] });
			toast.success('Workflow updated');
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Could not update workflow');
		}
	}

	async function toggleEnabled(wf: Workflow) {
		try {
			await workflows.update(wf.id, { is_enabled: !wf.is_enabled });
			client.invalidateQueries({ queryKey: ['workflows'] });
			toast.success(wf.is_enabled ? `Workflow “${wf.name}” disabled` : `Workflow “${wf.name}” enabled`);
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Could not update workflow');
		}
	}

	let deleteTarget = $state<Workflow | null>(null);
	let deleteOpen = $state(false);

	function askDelete(wf: Workflow) {
		deleteTarget = wf;
		deleteOpen = true;
	}

	async function confirmDelete() {
		const wf = deleteTarget;
		deleteTarget = null;
		if (!wf) return;
		try {
			await workflows.delete(wf.id);
			if (detailsId === wf.id) detailsId = null;
			client.invalidateQueries({ queryKey: ['workflows'] });
			toast.success(`Workflow “${wf.name}” deleted`);
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Could not delete workflow');
		}
	}

	let list = $derived($workflowsQuery.data ?? []);
	let imageCount = $derived(list.filter((w) => w.kind === 'image').length);
	let videoCount = $derived(list.filter((w) => w.kind === 'video').length);
</script>

<section class="block">
	<header class="block-head">
		<div>
			<h2>ComfyUI Workflows</h2>
			<p>
				Register API-format workflow JSONs as a reusable library. In ComfyUI, title editable
				nodes with role tags such as <code>(Input:prompt)</code>,
				<code>(Input:width)</code>, <code>(Output:image)</code> — see <code>AGENTS.md</code>.
			</p>
		</div>
		<div class="stats">
			<span class="stat">{list.length} saved</span>
			<span class="stat image">{imageCount} image</span>
			<span class="stat video">{videoCount} video</span>
		</div>
	</header>

	<div class="register panel">
		<h3>Analyze &amp; register</h3>
		<div
			class="drop"
			class:dragging
			role="button"
			tabindex="0"
			ondragover={(e) => {
				e.preventDefault();
				dragging = true;
			}}
			ondragleave={() => (dragging = false)}
			ondrop={onDrop}
			onclick={() => document.getElementById('wf-file')?.click()}
			onkeydown={(e) => e.key === 'Enter' && document.getElementById('wf-file')?.click()}
		>
			<input
				id="wf-file"
				type="file"
				accept="application/json,.json"
				hidden
				onchange={(e) => {
					const f = (e.currentTarget as HTMLInputElement).files?.[0];
					if (f) void onFile(f);
				}}
			/>
			<div class="drop-icon">JSON</div>
			<div>
				<strong>Drop ComfyUI API JSON here</strong>
				<p class="muted">or click to browse · Save (API Format) from ComfyUI</p>
			</div>
		</div>

		{#if uploadedFileName}
			<p class="file-name mono">{uploadedFileName}</p>
		{/if}

		{#if jsonText}
			<label class="field">
				<span class="field-label">JSON preview</span>
				<textarea class="field-textarea mono preview" rows="8" readonly value={jsonText}></textarea>
			</label>
			<Button variant="secondary" size="sm" onclick={() => analyzeRaw(jsonText)}>Re-analyze</Button>
		{/if}

		{#if parseError}
			<div class="error">{parseError}</div>
		{/if}

		{#if analyzed}
			<div class="io-panel">
				<div class="io-col">
					<h4>Detected inputs ({analyzedInputs.length})</h4>
					{#if analyzedInputs.length === 0}
						<p class="muted">None — add <code>(Input:prompt)</code> etc. to node titles in ComfyUI.</p>
					{:else}
						{#each analyzedInputs as inp}
							<div class="pill input">
								<span class="kind">{inp.kind}</span>
								{#if inp.role}<span class="kind">{inp.role}</span>{/if}
								<span>{inp.label}</span>
								<span class="node mono">#{inp.nodeId}</span>
							</div>
						{/each}
					{/if}
				</div>
				<div class="io-col">
					<h4>Detected outputs ({analyzedOutputs.length})</h4>
					{#if analyzedOutputs.length === 0}
						<p class="muted">None — add <code>(Output:image)</code> / <code>(Output:video)</code> to node titles.</p>
					{:else}
						{#each analyzedOutputs as out}
							<div class="pill output">
								<span class="kind">{out.kind}</span>
								{#if out.role}<span class="kind">{out.role}</span>{/if}
								<span>{out.label}</span>
								<span class="node mono">#{out.nodeId}</span>
							</div>
						{/each}
					{/if}
				</div>
			</div>

			<div class="meta-grid">
				<label class="field">
					<span class="field-label">Name</span>
					<input class="field-input" bind:value={wfName} placeholder="LTX Ref-to-Video" required />
				</label>
				<label class="field">
					<span class="field-label">Kind</span>
					<select class="field-select" bind:value={wfKind}>
						<option value="image">Image (Keyframe)</option>
						<option value="video">Video</option>
					</select>
				</label>
			</div>
			<label class="field">
				<span class="field-label">Prompt format</span>
				<select class="field-select" bind:value={wfProfile}>
					<option value="prose">Plain prose (default)</option>
					<option value="minimax_h3_ref">MiniMax H3 reference (6-section)</option>
				</select>
			</label>
			<label class="field">
				<span class="field-label">Description</span>
				<textarea
					class="field-textarea"
					bind:value={wfDescription}
					rows="2"
					placeholder="When to use this workflow…"
				></textarea>
			</label>
			<Button variant="primary" loading={saving} disabled={!wfName.trim()} onclick={saveToLibrary}>
				Save to library
			</Button>
		{/if}
	</div>

	<div class="library">
		<div class="library-head">
			<h3>Saved workflows ({list.length})</h3>
		</div>

		{#if $workflowsQuery.isLoading}
			<div class="cards" aria-busy="true" aria-label="Loading workflows">
				{#each [1, 2, 3] as n (n)}
					<div class="card skel-card">
						<Skeleton circle height="40px" />
						<div class="skel-lines">
							<Skeleton width="45%" height="15px" />
							<Skeleton width="70%" />
						</div>
					</div>
				{/each}
			</div>
		{:else if list.length === 0}
			<div class="empty">
				<strong>No workflows yet</strong>
				<p class="muted">Register at least one image and one video workflow to run the pipeline.</p>
			</div>
		{:else}
			<div class="cards">
				{#each list as wf (wf.id)}
					<article class="card" class:disabled={!wf.is_enabled}>
						{#if editingId === wf.id}
							<label class="field">
								<span class="field-label">Name</span>
								<input class="field-input" bind:value={editName} />
							</label>
							<label class="field">
								<span class="field-label">Description</span>
								<textarea class="field-textarea" rows="2" bind:value={editDescription}></textarea>
							</label>
							<label class="field">
								<span class="field-label">Prompt format</span>
								<select class="field-select" bind:value={editProfile}>
									<option value="prose">Plain prose (default)</option>
									<option value="minimax_h3_ref">MiniMax H3 reference (6-section)</option>
								</select>
							</label>
							<p class="field-hint">Workflow JSON is locked after registration.</p>
							<div class="row">
								<Button size="sm" onclick={() => saveEdit(wf.id)}>Save</Button>
								<Button variant="ghost" size="sm" onclick={() => (editingId = null)}>Cancel</Button>
							</div>
						{:else}
							<div class="card-top">
								<div class="tile" class:video={wf.kind === 'video'}>
									{wf.kind === 'video' ? 'VID' : 'IMG'}
								</div>
								<div class="card-meta">
									<strong>{wf.name}</strong>
									<div class="tags">
										<span class="kind-badge" class:video={wf.kind === 'video'}>{wf.kind}</span>
										{#if wf.prompt_profile === 'minimax_h3_ref'}
											<span class="kind-badge h3">H3-ref</span>
										{/if}
										<span class="count">{wf.input_schema?.length ?? 0} inputs</span>
										<span class="count">{wf.output_schema?.length ?? 0} outputs</span>
										{#if !wf.is_enabled}<StatusChip status="disabled" label="Disabled" />{/if}
									</div>
									{#if wf.description}
										<p class="desc">{wf.description}</p>
									{/if}
								</div>
							</div>
							<div class="row">
								<Button variant="ghost" size="sm" onclick={() => startEdit(wf)}>Edit</Button>
								<Button
									variant="ghost"
									size="sm"
									onclick={() => (detailsId = detailsId === wf.id ? null : wf.id)}
								>
									{detailsId === wf.id ? 'Hide I/O' : 'View I/O'}
								</Button>
								<Button variant="ghost" size="sm" onclick={() => toggleEnabled(wf)}>
									{wf.is_enabled ? 'Disable' : 'Enable'}
								</Button>
								<Button variant="danger" size="sm" onclick={() => askDelete(wf)}>Delete</Button>
							</div>
							{#if detailsId === wf.id}
								<div class="io-panel nested">
									<div class="io-col">
										<h4>Inputs</h4>
										{#each wf.input_schema ?? [] as inp}
											<div class="pill input">
												<span class="kind">{inp.kind}</span>
												{#if inp.role}<span class="kind">{inp.role}</span>{/if}
												<span>{inp.label}</span>
											</div>
										{:else}
											<p class="muted">No inputs cached.</p>
										{/each}
									</div>
									<div class="io-col">
										<h4>Outputs</h4>
										{#each wf.output_schema ?? [] as out}
											<div class="pill output">
												<span class="kind">{out.kind}</span>
												<span>{out.label}</span>
											</div>
										{:else}
											<p class="muted">No outputs cached.</p>
										{/each}
									</div>
								</div>
							{/if}
						{/if}
					</article>
				{/each}
			</div>
		{/if}
	</div>
</section>

<ConfirmDialog
	bind:open={deleteOpen}
	title="Delete workflow?"
	message={deleteTarget
		? `This removes “${deleteTarget.name}” from the library. This cannot be undone.`
		: ''}
	confirmLabel="Delete"
	danger
	onconfirm={confirmDelete}
	oncancel={() => (deleteTarget = null)}
/>

<style>
	.block-head {
		display: flex;
		justify-content: space-between;
		gap: var(--space-lg);
		align-items: flex-start;
		margin-bottom: var(--space-lg);
	}
	.block-head h2 {
		margin: 0 0 6px;
		font-size: 22px;
	}
	.block-head p {
		margin: 0;
		color: var(--text-secondary);
		font-size: 14px;
		max-width: 52ch;
		line-height: 1.45;
	}
	.block-head code {
		background: var(--bg-elevated);
		padding: 1px 6px;
		border-radius: 4px;
		font-size: 12px;
	}
	.stats {
		display: flex;
		gap: 8px;
		flex-wrap: wrap;
	}
	.stat {
		font-size: 11px;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		padding: 4px 10px;
		border-radius: 999px;
		background: var(--bg-elevated);
		color: var(--text-muted);
		border: 1px solid var(--border);
	}
	.stat.image {
		color: var(--accent);
		border-color: rgba(139, 92, 246, 0.35);
	}
	.stat.video {
		color: var(--info);
		border-color: rgba(59, 130, 246, 0.35);
	}
	.panel,
	.card {
		background: rgba(255, 255, 255, 0.03);
		border: 1px solid rgba(255, 255, 255, 0.08);
		border-radius: var(--radius-lg);
		padding: var(--space-lg);
	}
	.register {
		margin-bottom: var(--space-xl);
	}
	.register h3,
	.library-head h3 {
		margin: 0 0 var(--space-md);
		font-size: 16px;
		color: var(--text-secondary);
	}
	.drop {
		display: flex;
		align-items: center;
		gap: var(--space-md);
		padding: var(--space-lg);
		border: 1px dashed var(--border);
		border-radius: var(--radius-md);
		cursor: pointer;
		margin-bottom: var(--space-md);
		transition: all 0.15s;
	}
	.drop:hover,
	.drop.dragging {
		border-color: var(--accent);
		background: rgba(139, 92, 246, 0.08);
	}
	.drop:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.drop-icon {
		width: 44px;
		height: 44px;
		border-radius: var(--radius-md);
		background: rgba(139, 92, 246, 0.12);
		color: var(--accent);
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: 11px;
		font-weight: 700;
		font-family: var(--font-mono);
	}
	.muted {
		color: var(--text-muted);
		margin: 4px 0 0;
		font-size: 13px;
	}
	.file-name {
		font-size: 12px;
		color: var(--text-secondary);
		margin: 0 0 var(--space-md);
	}
	.preview {
		font-size: 11px;
		max-height: 180px;
	}
	.error {
		margin: var(--space-md) 0;
		padding: 10px 12px;
		border-radius: var(--radius-sm);
		background: rgba(239, 68, 68, 0.1);
		border: 1px solid rgba(239, 68, 68, 0.3);
		color: var(--error);
		font-size: 13px;
	}
	.io-panel {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: var(--space-md);
		margin: var(--space-md) 0;
		padding: var(--space-md);
		background: var(--bg-primary);
		border-radius: var(--radius-md);
		border: 1px solid var(--border);
	}
	.io-panel.nested {
		margin-top: var(--space-md);
	}
	@media (max-width: 800px) {
		.io-panel {
			grid-template-columns: 1fr;
		}
	}
	.io-col h4 {
		margin: 0 0 8px;
		font-size: 12px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--text-muted);
	}
	.pill {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 6px 8px;
		border-radius: var(--radius-sm);
		margin-bottom: 6px;
		font-size: 13px;
		background: var(--bg-elevated);
	}
	.pill .kind {
		font-size: 10px;
		font-weight: 700;
		text-transform: uppercase;
		padding: 2px 6px;
		border-radius: 999px;
	}
	.pill.input .kind {
		background: rgba(139, 92, 246, 0.2);
		color: var(--accent);
	}
	.pill.output .kind {
		background: rgba(34, 197, 94, 0.15);
		color: var(--success);
	}
	.pill .node {
		margin-left: auto;
		font-size: 11px;
		color: var(--text-muted);
	}
	.meta-grid {
		display: grid;
		grid-template-columns: 2fr 1fr;
		gap: var(--space-md);
	}
	.library-head {
		margin-bottom: var(--space-md);
	}
	.cards {
		display: flex;
		flex-direction: column;
		gap: var(--space-md);
	}
	.card.disabled {
		opacity: 0.55;
	}
	.card-top {
		display: flex;
		gap: var(--space-md);
		margin-bottom: var(--space-md);
	}
	.tile {
		width: 40px;
		height: 40px;
		border-radius: var(--radius-md);
		background: rgba(139, 92, 246, 0.12);
		color: var(--accent);
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: 10px;
		font-weight: 700;
		font-family: var(--font-mono);
		flex-shrink: 0;
	}
	.tile.video {
		background: rgba(59, 130, 246, 0.12);
		color: var(--info);
	}
	.card-meta strong {
		font-size: 15px;
		font-family: var(--font-display);
	}
	.tags {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		margin-top: 6px;
	}
	.kind-badge {
		font-size: 10px;
		font-weight: 700;
		text-transform: uppercase;
		padding: 2px 8px;
		border-radius: 999px;
		background: rgba(139, 92, 246, 0.15);
		color: var(--accent);
	}
	.kind-badge.video {
		background: rgba(59, 130, 246, 0.15);
		color: var(--info);
	}
	.kind-badge.h3 {
		background: rgba(34, 197, 94, 0.15);
		color: var(--success);
	}
	.count {
		font-size: 11px;
		color: var(--text-muted);
	}
	.desc {
		margin: 8px 0 0;
		font-size: 13px;
		color: var(--text-secondary);
	}
	.row {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}
	.empty {
		border: 1px dashed var(--border);
		border-radius: var(--radius-lg);
		padding: var(--space-xl);
		text-align: center;
	}
	.skel-card {
		display: flex;
		align-items: center;
		gap: var(--space-md);
	}
	.skel-lines {
		flex: 1;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
</style>
