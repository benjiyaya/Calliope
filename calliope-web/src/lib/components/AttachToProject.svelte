<script lang="ts">
	import { createMutation, createQuery } from '@tanstack/svelte-query';
	import {
		playgroundApi,
		projects,
		type Character,
		type Location,
		type PlaygroundAttachTarget,
		type Project,
		type Scene,
	} from '$lib/api';
	import { toast } from '$lib/toast';
	import Button from './ui/Button.svelte';
	import Icon from './ui/Icon.svelte';

	interface Props {
		path: string;
		kind: string;
	}

	let { path, kind }: Props = $props();

	const isVideo = $derived(
		kind === 'video' || path.toLowerCase().endsWith('.mp4') || path.toLowerCase().endsWith('.webm'),
	);

	let open = $state(false);
	let projectId = $state<number | ''>('');
	let target = $state<PlaygroundAttachTarget>('character_sheet');
	let characterId = $state<number | ''>('');
	let locationId = $state<number | ''>('');
	let itemName = $state('');
	let sceneId = $state<number | ''>('');
	let message = $state('');
	let characters = $state<Character[]>([]);
	let locations = $state<Location[]>([]);
	let scenes = $state<Scene[]>([]);
	let loadingTargets = $state(false);
	let attached = $state<{ projectId: number; title: string } | null>(null);

	const projectsQuery = createQuery({
		queryKey: ['projects'],
		queryFn: projects.list,
	});

	$effect(() => {
		if (!open) return;
		if (isVideo) {
			target = 'scene';
		} else if (target === 'scene') {
			target = 'character_sheet';
		}
	});

	$effect(() => {
		const pid = projectId;
		characterId = '';
		locationId = '';
		sceneId = '';
		message = '';
		characters = [];
		locations = [];
		scenes = [];
		if (!open || pid === '') return;

		let cancelled = false;
		loadingTargets = true;
		const load = async () => {
			try {
				const assets = await projects.getAssets(Number(pid));
				if (cancelled) return;
				characters = assets.characters;
				locations = assets.locations;
				if (isVideo) {
					const sceneData = await projects.getScenes(Number(pid));
					if (cancelled) return;
					scenes = sceneData.scenes;
				}
			} finally {
				if (!cancelled) loadingTargets = false;
			}
		};
		void load();
		return () => {
			cancelled = true;
		};
	});

	const attachMutation = createMutation({
		mutationFn: () => {
			if (projectId === '') throw new Error('Pick a project');
			const payload: Parameters<typeof playgroundApi.attach>[0] = {
				path,
				project_id: Number(projectId),
				target,
			};
			if (target === 'character_sheet') {
				if (characterId === '') throw new Error('Pick a character');
				payload.character_id = Number(characterId);
			} else if (target === 'location') {
				if (locationId === '') throw new Error('Pick a location');
				payload.location_id = Number(locationId);
			} else if (target === 'item') {
				const name = itemName.trim();
				if (name) payload.name = name;
			} else {
				if (sceneId === '') throw new Error('Pick a scene');
				payload.scene_id = Number(sceneId);
			}
			return playgroundApi.attach(payload);
		},
		onSuccess: (res) => {
			message = '';
			open = false;
			const p = projectList.find((x) => x.id === res.project_id);
			attached = { projectId: res.project_id, title: p?.title ?? `Project #${res.project_id}` };
			toast.success('Added to project');
		},
		onError: (err) => {
			message = err instanceof Error ? err.message : 'Attach failed';
			toast.error(message);
		},
	});

	function defaultMiscName(filePath: string): string {
		const base = filePath.replace(/\\/g, '/').split('/').pop() ?? '';
		const stem = base.replace(/\.[^.]+$/, '');
		const stripped = stem.replace(/^[0-9a-f]{8}-/i, '');
		const pretty = (stripped || stem).replace(/[_-]+/g, ' ').trim();
		return pretty || 'New item';
	}

	const projectList = $derived(($projectsQuery.data ?? []) as Project[]);

	// Stage-URL contract: images land on Assets, clips on Video.
	const stageLink = $derived(
		attached ? `/project/${attached.projectId}?stage=${isVideo ? 'video' : 'assets'}` : '',
	);
</script>

<div class="attach">
	{#if attached && !open}
		<div class="attached" role="status">
			<span class="attached-check" aria-hidden="true"><Icon name="check" size={14} /></span>
			<span class="attached-text">Added to <strong>{attached.title}</strong></span>
			<a class="attached-link" href={stageLink}>View in project</a>
			<button
				class="attached-dismiss"
				type="button"
				title="Dismiss"
				onclick={() => (attached = null)}
			>
				<Icon name="close" size={12} />
			</button>
		</div>
	{:else if !open}
		<Button
			variant="secondary"
			onclick={() => {
				itemName = defaultMiscName(path);
				open = true;
			}}
		>
			Add to project
		</Button>
	{:else}
		<div class="panel" role="group" aria-label="Add artifact to project">
			<label class="field">
				<span class="field-label">Project</span>
				<select class="field-select" bind:value={projectId}>
					<option value="">Select project…</option>
					{#each projectList as p}
						<option value={p.id}>{p.title}</option>
					{/each}
				</select>
			</label>

			{#if !isVideo}
				<label class="field">
					<span class="field-label">Add as</span>
					<select class="field-select" bind:value={target}>
						<option value="character_sheet">Character sheet</option>
						<option value="location">Background / location</option>
						<option value="item">Misc. item</option>
					</select>
				</label>

				{#if target === 'character_sheet'}
					<label class="field">
						<span class="field-label">Character</span>
						<select class="field-select" bind:value={characterId} disabled={projectId === '' || loadingTargets}>
							<option value="">Select character…</option>
							{#each characters as c}
								<option value={c.id}>{c.name}</option>
							{/each}
						</select>
						{#if projectId !== '' && !loadingTargets && characters.length === 0}
							<span class="field-hint">No characters in this project yet.</span>
						{/if}
					</label>
				{:else if target === 'location'}
					<label class="field">
						<span class="field-label">Location</span>
						<select class="field-select" bind:value={locationId} disabled={projectId === '' || loadingTargets}>
							<option value="">Select location…</option>
							{#each locations as loc}
								<option value={loc.id}>{loc.name}</option>
							{/each}
						</select>
						{#if projectId !== '' && !loadingTargets && locations.length === 0}
							<span class="field-hint">No locations in this project yet.</span>
						{/if}
					</label>
				{:else}
					<label class="field">
						<span class="field-label">Name</span>
						<input
							class="field-input"
							type="text"
							bind:value={itemName}
							placeholder="New misc. item"
						/>
						<span class="field-hint">Adds a new misc. item. Existing items are left unchanged.</span>
					</label>
				{/if}
			{:else}
				<label class="field">
					<span class="field-label">Scene</span>
					<select class="field-select" bind:value={sceneId} disabled={projectId === '' || loadingTargets}>
						<option value="">Select scene…</option>
						{#each scenes as s}
							<option value={s.id}>#{s.order_index} {s.heading || 'Scene'}</option>
						{/each}
					</select>
					{#if projectId !== '' && !loadingTargets && scenes.length === 0}
						<span class="field-hint">No scenes yet — generate a script first.</span>
					{/if}
				</label>
			{/if}

			<div class="actions">
				<Button
					variant="primary"
					loading={$attachMutation.isPending}
					disabled={projectId === ''}
					onclick={() => $attachMutation.mutate()}
				>
					Add
				</Button>
				<Button variant="ghost" onclick={() => (open = false)}>Cancel</Button>
			</div>
			{#if message && $attachMutation.isError}
				<p class="err" role="alert">{message}</p>
			{/if}
		</div>
	{/if}
</div>

<style>
	.attach {
		margin-top: 10px;
	}
	.attached {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 8px 10px;
		background: rgba(34, 197, 94, 0.08);
		border: 1px solid rgba(34, 197, 94, 0.3);
		border-radius: var(--radius-md);
		font-size: 12px;
		color: var(--text-secondary);
	}
	.attached-check {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 20px;
		height: 20px;
		border-radius: 50%;
		background: var(--success);
		color: #052e12;
		flex-shrink: 0;
	}
	.attached-text {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.attached-text strong {
		color: var(--text-primary);
		font-weight: 600;
	}
	.attached-link {
		color: var(--accent);
		font-weight: 600;
		text-decoration: none;
		white-space: nowrap;
		flex-shrink: 0;
	}
	.attached-link:hover {
		text-decoration: underline;
	}
	.attached-link:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
		border-radius: var(--radius-sm);
	}
	.attached-dismiss {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 22px;
		height: 22px;
		margin-left: auto;
		border: none;
		border-radius: var(--radius-sm);
		background: transparent;
		color: var(--text-muted);
		cursor: pointer;
		flex-shrink: 0;
	}
	.attached-dismiss:hover {
		color: var(--text-primary);
		background: rgba(255, 255, 255, 0.06);
	}
	.attached-dismiss:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.panel {
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 12px;
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
	}
	.actions {
		display: flex;
		gap: 8px;
		margin-top: 4px;
	}
	.err {
		margin: 4px 0 0;
		font-size: 12px;
		color: var(--error);
	}
</style>
