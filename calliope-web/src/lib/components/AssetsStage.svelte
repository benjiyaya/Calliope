<script lang="ts">
	import { goto } from '$app/navigation';
	import { toStore } from 'svelte/store';
	import { createMutation, createQuery, useQueryClient } from '@tanstack/svelte-query';
	import {
		assetUrl,
		jobsApi,
		libraryApi,
		playgroundApi,
		projects,
		settings as settingsApi,
		workflows,
		type Character,
		type Item,
		type Job,
		type Location,
		type Workflow,
	} from '$lib/api';
	import ComfyDynamicForm from '$lib/components/ComfyDynamicForm.svelte';
	import AssetThumb from '$lib/components/AssetThumb.svelte';
	import ImageLightbox from '$lib/components/ImageLightbox.svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import Card from '$lib/components/ui/Card.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';
	import ProgressBar from '$lib/components/ui/ProgressBar.svelte';
	import Skeleton from '$lib/components/ui/Skeleton.svelte';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import EmptyState from '$lib/components/ui/EmptyState.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';
	import {
		compactInputValues,
		withoutPromptInputs,
		workflowHasPromptInput,
	} from '$lib/comfy/promptInput';
	import { ROUTER_IMAGE_VALUE, routerModelOf } from '$lib/comfy/router';
	import {
		characterSheetTemplate,
		itemReferenceTemplate,
		locationReferenceTemplate,
	} from '$lib/promptTemplates';
	import { toast } from '$lib/toast';
	import { t } from '$lib/i18n.svelte';

	interface Props {
		projectId: number;
	}

	let { projectId }: Props = $props();
	const client = useQueryClient();
	let tab = $state<'characters' | 'locations' | 'items'>('characters');
	let drafts = $state<Record<string, string>>({});
	let savingKey = $state<string | null>(null);
	let sheetWorkflowId = $state<number | string>('');
	let envWorkflowId = $state<number | string>('');
	let itemWorkflowId = $state<number | string>('');
	let sheetInputValues = $state<Record<string, string | number>>({});
	let envInputValues = $state<Record<string, string | number>>({});
	let itemInputValues = $state<Record<string, string | number>>({});
	let sheetMissing = $state<string[]>([]);
	let envMissing = $state<string[]>([]);
	let itemMissing = $state<string[]>([]);
	let sheetAttempted = $state(false);
	let envAttempted = $state(false);
	let itemAttempted = $state(false);
	let lastSheetWf = $state<number | string | null>(null);
	let previewSrc = $state<string | null>(null);
	let previewAlt = $state('');

	function openPreview(src: string | null | undefined, alt: string) {
		if (!src) return;
		previewSrc = src;
		previewAlt = alt;
	}
	let lastEnvWf = $state<number | string | null>(null);
	let lastItemWf = $state<number | string | null>(null);
	let bulkPending = $state(false);
	let createAssetOpen = $state(false);
	let createAssetKind = $state<'character' | 'location' | 'item'>('character');
	let createAssetName = $state('');
	let createAssetDescription = $state('');
	let createAssetRole = $state('');
	let createAssetAge = $state('');
	let createAssetPersonality = $state('');
	let createAssetImagePath = $state<string | null>(null);
	let uploadingCreateAssetImage = $state(false);
	let createAssetFileInput: HTMLInputElement | null = $state(null);
	let editAssetOpen = $state(false);
	let editAssetKind = $state<'character' | 'location' | 'item'>('character');
	let editAssetId = $state<number | null>(null);
	let editAssetName = $state('');
	let editAssetDescription = $state('');
	let editAssetRole = $state('');
	let editAssetAge = $state('');
	let editAssetPersonality = $state('');
	let editAssetImagePath = $state<string | null>(null);
	let editAssetImageChanged = $state(false);

	type DeleteTarget = { kind: 'character' | 'location' | 'item'; id: number; name: string };
	let deleteTarget = $state<DeleteTarget | null>(null);
	let deleteOpen = $state(false);
	let fileInput: HTMLInputElement | null = $state(null);
	let uploadTarget = $state<DeleteTarget | null>(null);
	let uploadingKey = $state<string | null>(null);

	const assetsQuery = createQuery(
		toStore(() => ({
			queryKey: ['assets', projectId],
			queryFn: () => projects.getAssets(projectId),
		})),
	);

	const libraryMediaQuery = createQuery(
		toStore(() => ({
			queryKey: ['library-media'],
			queryFn: libraryApi.list,
			enabled: createAssetOpen || editAssetOpen,
		})),
	);

	const workflowsQuery = createQuery({
		queryKey: ['workflows'],
		queryFn: workflows.list,
	});
	const settingsQuery = createQuery({
		queryKey: ['settings'],
		queryFn: settingsApi.get,
	});
		const fluxOption = $derived(
		$settingsQuery.data?.comfy_api_key && $settingsQuery.data?.comfy_router_enabled
			? { value: ROUTER_IMAGE_VALUE, label: t('router.flux3') }
			: null,
	);

	// Shared cache key with QueueStage — refreshed via SSE invalidation from
	// the project page (job.* events), not polling.
	const jobsQuery = createQuery(
		toStore(() => ({
			queryKey: ['jobs', projectId],
			queryFn: () => jobsApi.list(projectId),
		})),
	);

	// Project cover: full asset path (as stored in the DB) chosen as the project thumbnail.
	const projectQuery = createQuery(
		toStore(() => ({
			queryKey: ['project', projectId],
			queryFn: () => projects.get(projectId),
		})),
	);

	const coverPath = $derived($projectQuery.data?.cover_path ?? null);

	const coverMutation = createMutation({
		mutationFn: (path: string | null) => projects.update(projectId, { cover_path: path }),
		onSuccess: async (updated) => {
			await client.invalidateQueries({ queryKey: ['project'] });
			await client.invalidateQueries({ queryKey: ['projects'] });
			toast.success(
				updated.cover_path ? t('assets.toast.coverUpdated') : t('assets.toast.coverRemoved'),
			);
		},
		onError: (err) => {
			toast.error(err instanceof Error ? err.message : t('assets.toast.coverFail'));
		},
	});

	/** True when this asset image is the current project cover. */
	function isCover(path: string | null): boolean {
		return path !== null && coverPath === path;
	}

	/** Same action sets and clears: clicking the current cover removes it. */
	function toggleCover(path: string | null) {
		if (!path) return;
		$coverMutation.mutate(coverPath === path ? null : path);
	}

	const imageWorkflows = $derived(
		(($workflowsQuery.data ?? []) as Workflow[]).filter((w) => w.is_enabled && w.kind === 'image'),
	);

	const sheetWorkflow = $derived(
		imageWorkflows.find((w) => w.id === sheetWorkflowId) ?? null,
	);
	const envWorkflow = $derived(imageWorkflows.find((w) => w.id === envWorkflowId) ?? null);
	const itemWorkflow = $derived(imageWorkflows.find((w) => w.id === itemWorkflowId) ?? null);

	const sheetHasPrompt = $derived(workflowHasPromptInput(sheetWorkflow?.input_schema));
	const envHasPrompt = $derived(workflowHasPromptInput(envWorkflow?.input_schema));
	const itemHasPrompt = $derived(workflowHasPromptInput(itemWorkflow?.input_schema));
	const showCharPrompt = $derived(sheetHasPrompt || !!routerModelOf(sheetWorkflowId));

	const jobs = $derived($jobsQuery.data ?? []);

	const assetOptions = $derived.by(() => {
		const chars = $assetsQuery.data?.characters ?? [];
		const locs = $assetsQuery.data?.locations ?? [];
		const items = $assetsQuery.data?.items ?? [];
		const opts: Array<{ label: string; path: string; group?: 'character' | 'location' | 'item' }> =
			[];
		for (const c of chars) {
			if (c.sheet_path) {
				opts.push({
					label: `${c.name} · ${t('assets.optionSheet')}`,
					path: c.sheet_path,
					group: 'character',
				});
			}
		}
		for (const loc of locs) {
			if (loc.reference_image_path) {
				opts.push({
					label: `${loc.name} · ${t('assets.optionEnvironment')}`,
					path: loc.reference_image_path,
					group: 'location',
				});
			}
		}
		for (const it of items) {
			if (it.reference_image_path) {
				opts.push({
					label: `${it.name} · ${t('assets.optionItem')}`,
					path: it.reference_image_path,
					group: 'item',
				});
			}
		}
		return opts;
	});

	$effect(() => {
		const list = imageWorkflows;
		if (list.length === 0) return;
		const first = list[0].id;
		if (sheetWorkflowId === '') sheetWorkflowId = first;
		if (envWorkflowId === '') envWorkflowId = first;
		if (itemWorkflowId === '') itemWorkflowId = first;
	});

	$effect(() => {
		if (sheetWorkflowId === lastSheetWf) return;
		lastSheetWf = sheetWorkflowId;
		sheetInputValues = {};
		sheetAttempted = false;
	});
	$effect(() => {
		if (envWorkflowId === lastEnvWf) return;
		lastEnvWf = envWorkflowId;
		envInputValues = {};
		envAttempted = false;
	});
	$effect(() => {
		if (itemWorkflowId === lastItemWf) return;
		lastItemWf = itemWorkflowId;
		itemInputValues = {};
		itemAttempted = false;
	});

	const generateMutation = createMutation({
		mutationFn: (payload: {
			missing_only?: boolean;
			character_ids?: number[];
			location_ids?: number[];
			item_ids?: number[];
			workflow_id?: number;
			input_values?: Record<string, unknown>;
			asset_target?: 'sheet';
			prompt?: string;
			router_model?: string;
		}) => projects.generateAssets(projectId, payload),
		onSuccess: async () => {
			await client.invalidateQueries({ queryKey: ['assets'] });
			await client.invalidateQueries({ queryKey: ['jobs'] });
			await client.invalidateQueries({ queryKey: ['story'] });
			toast.success(t('assets.toast.queued'));
		},
		onError: (err) => {
			toast.error(err instanceof Error ? err.message : String(err));
		},
	});

	const retryJobMutation = createMutation({
		mutationFn: (jobId: number) => jobsApi.retry(jobId),
		onSuccess: () => {
			client.invalidateQueries({ queryKey: ['jobs'] });
			toast.success(t('assets.toast.jobRequeued'));
		},
		onError: (err) => {
			toast.error(err instanceof Error ? err.message : t('assets.toast.retryFailed'));
		},
	});

	const deleteEntityMutation = createMutation({
		mutationFn: (target: DeleteTarget) =>
			target.kind === 'character'
				? projects.deleteCharacter(projectId, target.id)
				: target.kind === 'location'
					? projects.deleteLocation(projectId, target.id)
					: projects.deleteItem(projectId, target.id),
		onSuccess: async (_data, target) => {
			await client.invalidateQueries({ queryKey: ['assets'] });
			await client.invalidateQueries({ queryKey: ['story'] });
			await client.invalidateQueries({ queryKey: ['scenes'] });
			toast.success(t('assets.toast.deleted', { name: target.name }));
		},
		onError: (err) => {
			toast.error(err instanceof Error ? err.message : t('assets.toast.deleteFailed'));
		},
	});

	const createEntityMutation = createMutation({
		mutationFn: async ({
			kind,
			name,
			description,
			role,
			age,
			personality,
		}: {
			kind: 'character' | 'location' | 'item';
			name: string;
			description: string;
			imagePath: string | null;
			role: string;
			age: string;
			personality: string;
		}) => {
			if (kind === 'character')
				return projects.createCharacter(projectId, {
					name,
					role: role || undefined,
					age: age || undefined,
					appearance: description || undefined,
					personality: personality || undefined,
				});
			if (kind === 'location')
				return projects.createLocation(projectId, { name, description: description || undefined });
			return projects.createItem(projectId, { name, description: description || undefined });
		},
		onSuccess: async (asset, { kind, name, imagePath }) => {
			let imageError: unknown = null;
			if (imagePath) {
				try {
					if (kind === 'character') {
						await projects.updateCharacter(projectId, asset.id, { sheet_path: imagePath });
					} else if (kind === 'location') {
						await projects.updateLocation(projectId, asset.id, {
							reference_image_path: imagePath,
						});
					} else {
						await projects.updateItem(projectId, asset.id, {
							reference_image_path: imagePath,
						});
					}
				} catch (err) {
					imageError = err;
				}
			}
			createAssetOpen = false;
			createAssetName = '';
			createAssetDescription = '';
			createAssetRole = '';
			createAssetAge = '';
			createAssetPersonality = '';
			createAssetImagePath = null;
			await client.invalidateQueries({ queryKey: ['assets', projectId] });
			await client.invalidateQueries({ queryKey: ['story', projectId] });
			await client.invalidateQueries({ queryKey: ['scenes', projectId] });
			if (imageError) {
				toast.error(
					t('assets.toast.imageAttachFailed', {
						error: imageError instanceof Error ? imageError.message : String(imageError),
					}),
				);
			} else {
				toast.success(t('assets.toast.created', { kind: kindNoun(kind), name }));
			}
		},
		onError: (err) => {
			toast.error(err instanceof Error ? err.message : t('assets.toast.createFailed'));
		},
	});

	const updateEntityMutation = createMutation({
		mutationFn: async ({
			kind,
			id,
			name,
			description,
			role,
			age,
			personality,
			imagePath,
			imageChanged,
		}: {
			kind: 'character' | 'location' | 'item';
			id: number;
			name: string;
			description: string;
			role: string;
			age: string;
			personality: string;
			imagePath: string | null;
			imageChanged: boolean;
		}) => {
			if (kind === 'character') {
				return projects.updateCharacter(projectId, id, {
					name,
					role: role || null,
					age: age || null,
					appearance: description || null,
					personality: personality || null,
					...(imageChanged ? { sheet_path: imagePath } : {}),
				});
			}
			const payload = {
				name,
				description: description || null,
				...(imageChanged ? { reference_image_path: imagePath } : {}),
			};
			return kind === 'location'
				? projects.updateLocation(projectId, id, payload)
				: projects.updateItem(projectId, id, payload);
		},
		onSuccess: async () => {
			editAssetOpen = false;
			editAssetId = null;
			await client.invalidateQueries({ queryKey: ['assets', projectId] });
			await client.invalidateQueries({ queryKey: ['story', projectId] });
			await client.invalidateQueries({ queryKey: ['scenes', projectId] });
			toast.success(t('assets.toast.updated'));
		},
		onError: (err) => {
			toast.error(err instanceof Error ? err.message : t('assets.toast.updateFailed'));
		},
	});

	const busy = $derived(bulkPending || $generateMutation.isPending);

	/** Latest image job for one entity — drives the on-card generation state. */
	function entityJob(
		list: Job[],
		key: 'character_id' | 'location_id' | 'item_id',
		id: number,
	): Job | undefined {
		const matches = list.filter((j) => j.kind === 'image' && j.payload?.[key] === id);
		if (matches.length === 0) return undefined;
		return matches.sort((a, b) => b.id - a.id)[0];
	}

	function jobStateOf(job: Job | undefined): 'generating' | 'failed' | null {
		if (!job) return null;
		if (job.status === 'pending' || job.status === 'running') return 'generating';
		if (job.status === 'failed') return 'failed';
		return null;
	}

	function charKey(id: number) {
		return `c:${id}`;
	}
	function locKey(id: number) {
		return `l:${id}`;
	}
	function itemKey(id: number) {
		return `i:${id}`;
	}

	function promptForChar(c: Character): string {
		const k = charKey(c.id);
		if (drafts[k] !== undefined) return drafts[k];
		return c.consistency_prompt?.trim() || characterSheetTemplate(c);
	}

	function promptForLoc(loc: Location): string {
		const k = locKey(loc.id);
		if (drafts[k] !== undefined) return drafts[k];
		return loc.consistency_prompt?.trim() || locationReferenceTemplate(loc);
	}

	function promptForItem(item: Item): string {
		const k = itemKey(item.id);
		if (drafts[k] !== undefined) return drafts[k];
		return item.consistency_prompt?.trim() || itemReferenceTemplate(item);
	}

	function setDraft(key: string, value: string) {
		drafts = { ...drafts, [key]: value };
	}

	function requireWorkflow(id: number | string, needKey: string): number | null {
		if (id === '' || routerModelOf(id)) {
			if (id === '') toast.error(t(needKey));
			return null;
		}
		const n = Number(id);
		if (!Number.isFinite(n)) {
			toast.error(t(needKey));
			return null;
		}
		return n;
	}

	function pickWorkflow(value: string): number | string {
		if (!value) return '';
		return routerModelOf(value) ? value : Number(value);
	}

	function requireComplete(missing: string[], markAttempted: () => void): boolean {
		if (missing.length === 0) return true;
		markAttempted();
		toast.error(t('assets.wf.missingInputs', { list: missing.join(', ') }));
		return false;
	}

	function kindNoun(kind: DeleteTarget['kind']): string {
		return kind === 'character'
			? t('assets.kind.character')
			: kind === 'location'
				? t('assets.kind.environment')
				: t('assets.kind.item');
	}

	function openCreateAsset() {
		createAssetKind = tab === 'characters' ? 'character' : tab === 'locations' ? 'location' : 'item';
		createAssetName = '';
		createAssetDescription = '';
		createAssetRole = '';
		createAssetAge = '';
		createAssetPersonality = '';
		createAssetImagePath = null;
		createAssetOpen = true;
	}

	function openEditCharacter(char: Character) {
		editAssetKind = 'character';
		editAssetId = char.id;
		editAssetName = char.name;
		editAssetRole = char.role ?? '';
		editAssetAge = char.age ?? '';
		editAssetDescription = char.appearance ?? '';
		editAssetPersonality = char.personality ?? '';
		editAssetImagePath = char.sheet_path;
		editAssetImageChanged = false;
		editAssetOpen = true;
	}

	function openEditLocation(loc: Location) {
		editAssetKind = 'location';
		editAssetId = loc.id;
		editAssetName = loc.name;
		editAssetDescription = loc.description ?? '';
		editAssetRole = '';
		editAssetAge = '';
		editAssetPersonality = '';
		editAssetImagePath = loc.reference_image_path;
		editAssetImageChanged = false;
		editAssetOpen = true;
	}

	function openEditItem(item: Item) {
		editAssetKind = 'item';
		editAssetId = item.id;
		editAssetName = item.name;
		editAssetDescription = item.description ?? '';
		editAssetRole = '';
		editAssetAge = '';
		editAssetPersonality = '';
		editAssetImagePath = item.reference_image_path;
		editAssetImageChanged = false;
		editAssetOpen = true;
	}

	function saveEditedAsset() {
		const name = editAssetName.trim();
		if (!name || editAssetId === null || $updateEntityMutation.isPending || uploadingCreateAssetImage)
			return;
		$updateEntityMutation.mutate({
			kind: editAssetKind,
			id: editAssetId,
			name,
			description: editAssetDescription.trim(),
			role: editAssetRole.trim(),
			age: editAssetAge.trim(),
			personality: editAssetPersonality.trim(),
			imagePath: editAssetImagePath,
			imageChanged: editAssetImageChanged,
		});
	}

	function createAsset() {
		const name = createAssetName.trim();
		if (!name || $createEntityMutation.isPending || uploadingCreateAssetImage) return;
		$createEntityMutation.mutate({
			kind: createAssetKind,
			name,
			description: createAssetDescription.trim(),
			role: createAssetRole.trim(),
			age: createAssetAge.trim(),
			personality: createAssetPersonality.trim(),
			imagePath: createAssetImagePath,
		});
	}

	async function uploadCreateAssetImage(event: Event) {
		const input = event.currentTarget as HTMLInputElement;
		const file = input.files?.[0];
		input.value = '';
		if (!file) return;
		if (!file.type.startsWith('image/')) {
			toast.error(t('assets.toast.chooseImage'));
			return;
		}
		uploadingCreateAssetImage = true;
		try {
			const uploaded = await playgroundApi.upload(file);
			if (uploaded.kind !== 'image') {
				toast.error(t('assets.toast.notImage'));
				return;
			}
			if (editAssetOpen) {
				editAssetImagePath = uploaded.path;
				editAssetImageChanged = true;
			} else {
				createAssetImagePath = uploaded.path;
			}
			await client.invalidateQueries({ queryKey: ['library-media'] });
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('assets.toast.addImageFailed'));
		} finally {
			uploadingCreateAssetImage = false;
		}
	}

	function requestDelete(target: DeleteTarget) {
		deleteTarget = target;
		deleteOpen = true;
	}

	async function saveCharacterPrompt(c: Character) {
		const key = charKey(c.id);
		const text = promptForChar(c);
		savingKey = key;
		try {
			await projects.updateCharacter(projectId, c.id, { consistency_prompt: text });
			const { [key]: _, ...rest } = drafts;
			drafts = rest;
			await client.invalidateQueries({ queryKey: ['assets'] });
			await client.invalidateQueries({ queryKey: ['story'] });
			toast.success(t('assets.toast.savedPrompt', { name: c.name }));
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('assets.toast.savePromptFailed'));
		} finally {
			savingKey = null;
		}
	}

	async function saveLocationPrompt(loc: Location) {
		const key = locKey(loc.id);
		const text = promptForLoc(loc);
		savingKey = key;
		try {
			await projects.updateLocation(projectId, loc.id, { consistency_prompt: text });
			const { [key]: _, ...rest } = drafts;
			drafts = rest;
			await client.invalidateQueries({ queryKey: ['assets'] });
			await client.invalidateQueries({ queryKey: ['story'] });
			toast.success(t('assets.toast.savedPrompt', { name: loc.name }));
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('assets.toast.savePromptFailed'));
		} finally {
			savingKey = null;
		}
	}

	async function saveItemPrompt(item: Item) {
		const key = itemKey(item.id);
		const text = promptForItem(item);
		savingKey = key;
		try {
			await projects.updateItem(projectId, item.id, { consistency_prompt: text });
			const { [key]: _, ...rest } = drafts;
			drafts = rest;
			await client.invalidateQueries({ queryKey: ['assets'] });
			await client.invalidateQueries({ queryKey: ['story'] });
			toast.success(t('assets.toast.savedPrompt', { name: item.name }));
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('assets.toast.savePromptFailed'));
		} finally {
			savingKey = null;
		}
	}

	async function generateCharacter(c: Character) {
		const model = routerModelOf(sheetWorkflowId);
		let wfId: number | undefined;
		if (!model) {
			const required = requireWorkflow(sheetWorkflowId, 'assets.wf.needSheet');
			if (required == null) return;
			if (!requireComplete(sheetMissing, () => (sheetAttempted = true))) return;
			wfId = required;
		}
		const prompt = model || sheetHasPrompt ? promptForChar(c) : '';
		if (prompt) {
			await projects.updateCharacter(projectId, c.id, { consistency_prompt: prompt });
			const { [charKey(c.id)]: _, ...rest } = drafts;
			drafts = rest;
		}
		$generateMutation.mutate({
			missing_only: false,
			character_ids: [c.id],
			location_ids: [],
			asset_target: 'sheet',
			workflow_id: wfId,
			router_model: model ?? undefined,
			input_values: compactInputValues(sheetInputValues),
			prompt: prompt || undefined,
		});
	}

	async function generateLocation(loc: Location) {
		const model = routerModelOf(envWorkflowId);
		let wfId: number | undefined;
		if (!model) {
			const required = requireWorkflow(envWorkflowId, 'assets.wf.needEnv');
			if (required == null) return;
			if (!requireComplete(envMissing, () => (envAttempted = true))) return;
			wfId = required;
		}
		const prompt = model || envHasPrompt ? promptForLoc(loc) : '';
		if (prompt) {
			await projects.updateLocation(projectId, loc.id, { consistency_prompt: prompt });
			const { [locKey(loc.id)]: _, ...rest } = drafts;
			drafts = rest;
		}
		$generateMutation.mutate({
			missing_only: false,
			character_ids: [],
			location_ids: [loc.id],
			workflow_id: wfId,
			router_model: model ?? undefined,
			input_values: compactInputValues(envInputValues),
			prompt: prompt || undefined,
		});
	}

	async function generateItem(item: Item) {
		const model = routerModelOf(itemWorkflowId);
		let wfId: number | undefined;
		if (!model) {
			const required = requireWorkflow(itemWorkflowId, 'assets.wf.needItem');
			if (required == null) return;
			if (!requireComplete(itemMissing, () => (itemAttempted = true))) return;
			wfId = required;
		}
		const prompt = model || itemHasPrompt ? promptForItem(item) : '';
		if (prompt) {
			await projects.updateItem(projectId, item.id, { consistency_prompt: prompt });
			const { [itemKey(item.id)]: _, ...rest } = drafts;
			drafts = rest;
		}
		$generateMutation.mutate({
			missing_only: false,
			character_ids: [],
			location_ids: [],
			item_ids: [item.id],
			workflow_id: wfId,
			router_model: model ?? undefined,
			input_values: compactInputValues(itemInputValues),
			prompt: prompt || undefined,
		});
	}

	async function generateMissing() {
		const chars = $assetsQuery.data?.characters ?? [];
		const locs = $assetsQuery.data?.locations ?? [];
		const items = $assetsQuery.data?.items ?? [];
		const sheetModel = routerModelOf(sheetWorkflowId);
		const envModel = routerModelOf(envWorkflowId);
		const itemModel = routerModelOf(itemWorkflowId);
		const sheetWf = sheetModel || sheetWorkflowId === '' ? null : Number(sheetWorkflowId);
		const envWf = envModel || envWorkflowId === '' ? null : Number(envWorkflowId);
		const itemWf = itemModel || itemWorkflowId === '' ? null : Number(itemWorkflowId);

		if (
			!sheetModel &&
			!envModel &&
			!itemModel &&
			sheetWf == null &&
			envWf == null &&
			itemWf == null
		) {
			toast.error(t('assets.wf.needWorkflow'));
			return;
		}

		bulkPending = true;
		try {
			let total = 0;
			if ((sheetModel || sheetWf != null) && chars.length > 0) {
				const r = await projects.generateAssets(projectId, {
					missing_only: true,
					asset_target: 'sheet',
					workflow_id: sheetModel ? undefined : (sheetWf ?? undefined),
					router_model: sheetModel ?? undefined,
					character_ids: chars.map((c) => c.id),
					input_values: compactInputValues(sheetInputValues),
				});
				total += r.jobs.length;
			}
			if ((envModel || envWf != null) && locs.length > 0) {
				const r = await projects.generateAssets(projectId, {
					missing_only: true,
					workflow_id: envModel ? undefined : (envWf ?? undefined),
					router_model: envModel ?? undefined,
					character_ids: [],
					location_ids: locs.map((l) => l.id),
					input_values: compactInputValues(envInputValues),
				});
				total += r.jobs.length;
			}
			if ((itemModel || itemWf != null) && items.length > 0) {
				const r = await projects.generateAssets(projectId, {
					missing_only: true,
					workflow_id: itemModel ? undefined : (itemWf ?? undefined),
					router_model: itemModel ?? undefined,
					character_ids: [],
					location_ids: [],
					item_ids: items.map((it) => it.id),
					input_values: compactInputValues(itemInputValues),
				});
				total += r.jobs.length;
			}
			await client.invalidateQueries({ queryKey: ['assets'] });
			await client.invalidateQueries({ queryKey: ['jobs'] });
			await client.invalidateQueries({ queryKey: ['story'] });
			toast.success(
				total > 0
					? total === 1
						? t('assets.toast.queuedOne')
						: t('assets.toast.queuedMany', { count: total })
					: t('assets.toast.nothingMissing'),
			);
		} catch (err) {
			toast.error(err instanceof Error ? err.message : String(err));
		} finally {
			bulkPending = false;
		}
	}

	function goToStory() {
		goto('?stage=story', { keepFocus: true, noScroll: true });
	}

	function uploadKey(kind: DeleteTarget['kind'], id: number) {
		if (kind === 'character') return charKey(id);
		if (kind === 'location') return locKey(id);
		return itemKey(id);
	}

	function openOwnImage(target: DeleteTarget) {
		uploadTarget = target;
		fileInput?.click();
	}

	async function onOwnImageChosen(e: Event) {
		const input = e.currentTarget as HTMLInputElement;
		const file = input.files?.[0];
		input.value = '';
		const target = uploadTarget;
		uploadTarget = null;
		if (!file || !target) return;
		if (!file.type.startsWith('image/')) {
			toast.error(t('assets.toast.chooseImage'));
			return;
		}
		const key = uploadKey(target.kind, target.id);
		uploadingKey = key;
		try {
			const uploaded = await playgroundApi.upload(file);
			if (uploaded.kind !== 'image') {
				toast.error(t('assets.toast.notImage'));
				return;
			}
			if (target.kind === 'character') {
				await projects.updateCharacter(projectId, target.id, { sheet_path: uploaded.path });
			} else if (target.kind === 'location') {
				await projects.updateLocation(projectId, target.id, {
					reference_image_path: uploaded.path,
				});
			} else {
				await projects.updateItem(projectId, target.id, { reference_image_path: uploaded.path });
			}
			await client.invalidateQueries({ queryKey: ['assets', projectId] });
			await client.invalidateQueries({ queryKey: ['project', projectId] });
			await client.invalidateQueries({ queryKey: ['projects'] });
			toast.success(t('assets.toast.imageAdded', { name: target.name }));
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('assets.toast.addImageFailed'));
		} finally {
			uploadingKey = null;
		}
	}
</script>

<input
	bind:this={fileInput}
	type="file"
	class="sr-only"
	accept="image/png,image/jpeg,image/webp,image/gif,image/bmp"
	onchange={onOwnImageChosen}
/>
<input
	bind:this={createAssetFileInput}
	type="file"
	class="sr-only"
	accept="image/png,image/jpeg,image/webp,image/gif,image/bmp"
	onchange={uploadCreateAssetImage}
/>

<header class="stage-header">
	<div>
		<h2>{t('assets.title')}</h2>
		<p class="lead">{t('assets.lead')}</p>
	</div>
	<div class="stage-actions">
		<Button variant="secondary" onclick={openCreateAsset}>
			<Icon name="plus" size={14} /> {t('assets.addAsset')}
		</Button>
		<Button
			variant="primary"
			disabled={busy}
			title={t('assets.generateMissingTitle')}
			onclick={generateMissing}
		>
			{busy ? t('assets.queuing') : t('assets.generateMissing')}
		</Button>
	</div>
</header>

<div class="tabs-row">
	<div class="tabs" role="tablist" aria-label={t('assets.tabAria')}>
		<button
			type="button"
			role="tab"
			aria-selected={tab === 'characters'}
			class:active={tab === 'characters'}
			onclick={() => (tab = 'characters')}
			>{t('assets.tabCharacters')}{#if $assetsQuery.data}
				· {$assetsQuery.data.characters.length}{/if}</button
		>
		<button
			type="button"
			role="tab"
			aria-selected={tab === 'locations'}
			class:active={tab === 'locations'}
			onclick={() => (tab = 'locations')}
			>{t('assets.tabEnvironments')}{#if $assetsQuery.data}
				· {$assetsQuery.data.locations.length}{/if}</button
		>
		<button
			type="button"
			role="tab"
			aria-selected={tab === 'items'}
			class:active={tab === 'items'}
			onclick={() => (tab = 'items')}
			>{t('assets.tabItems')}{#if $assetsQuery.data}
				· {$assetsQuery.data.items.length}{/if}</button
		>
	</div>
</div>

{#if imageWorkflows.length === 0 && !fluxOption}
	<p class="hint-warn">
		{t('assets.noWorkflowPre')} <a href="/settings?tab=workflows">{t('assets.noWorkflowLink')}</a>
		{t('assets.noWorkflowPost')}
	</p>
{/if}

<div class="settings-wrap">
	<Card>
	{#snippet header()}
		<h3 class="card-h">{t('assets.genSettings')}</h3>
		<span class="muted small"
			>{tab === 'characters'
				? t('assets.charSheets')
				: tab === 'locations'
					? t('assets.envRefs')
					: t('assets.itemRefs')}</span
		>
	{/snippet}
	{#if tab === 'characters'}
		<label class="wf-field">
			<span class="field-label">{t('assets.wf.sheet')}</span>
			<select
				class="field-select"
				value={sheetWorkflowId}
				onchange={(e) => (sheetWorkflowId = pickWorkflow(e.currentTarget.value))}
			>
				{#if imageWorkflows.length === 0}
					<option value="">{t('assets.wf.none')}</option>
				{/if}
				{#each imageWorkflows as w (w.id)}
					<option value={w.id}>{w.name}</option>
				{/each}
				{#if fluxOption}
					<option value={fluxOption.value}>{fluxOption.label}</option>
				{/if}
			</select>
		</label>
		{#if sheetWorkflow}
			<ComfyDynamicForm
				inputs={withoutPromptInputs(sheetWorkflow.input_schema)}
				bind:values={sheetInputValues}
				assetOptions={assetOptions}
				showErrors={sheetAttempted}
				onValidityChange={(m) => (sheetMissing = m)}
			/>
		{:else if routerModelOf(sheetWorkflowId)}
			<p class="muted small">{t('router.fluxHint')}</p>
		{:else}
			<p class="muted small">{t('assets.wf.selectSheet')}</p>
		{/if}
	{:else if tab === 'locations'}
		<label class="wf-field">
			<span class="field-label">{t('assets.wf.environment')}</span>
			<select
				class="field-select"
				value={envWorkflowId}
				onchange={(e) => (envWorkflowId = pickWorkflow(e.currentTarget.value))}
			>
				{#if imageWorkflows.length === 0}
					<option value="">{t('assets.wf.none')}</option>
				{/if}
				{#each imageWorkflows as w (w.id)}
					<option value={w.id}>{w.name}</option>
				{/each}
				{#if fluxOption}
					<option value={fluxOption.value}>{fluxOption.label}</option>
				{/if}
			</select>
		</label>
		{#if envWorkflow}
			<ComfyDynamicForm
				inputs={withoutPromptInputs(envWorkflow.input_schema)}
				bind:values={envInputValues}
				assetOptions={assetOptions}
				showErrors={envAttempted}
				onValidityChange={(m) => (envMissing = m)}
			/>
		{:else if routerModelOf(envWorkflowId)}
			<p class="muted small">{t('router.fluxHint')}</p>
		{:else}
			<p class="muted small">{t('assets.wf.selectEnv')}</p>
		{/if}
	{:else}
		<label class="wf-field">
			<span class="field-label">{t('assets.wf.item')}</span>
			<select
				class="field-select"
				value={itemWorkflowId}
				onchange={(e) => (itemWorkflowId = pickWorkflow(e.currentTarget.value))}
			>
				{#if imageWorkflows.length === 0}
					<option value="">{t('assets.wf.none')}</option>
				{/if}
				{#each imageWorkflows as w (w.id)}
					<option value={w.id}>{w.name}</option>
				{/each}
				{#if fluxOption}
					<option value={fluxOption.value}>{fluxOption.label}</option>
				{/if}
			</select>
		</label>
		{#if itemWorkflow}
			<ComfyDynamicForm
				inputs={withoutPromptInputs(itemWorkflow.input_schema)}
				bind:values={itemInputValues}
				assetOptions={assetOptions}
				showErrors={itemAttempted}
				onValidityChange={(m) => (itemMissing = m)}
			/>
		{:else if routerModelOf(itemWorkflowId)}
			<p class="muted small">{t('router.fluxHint')}</p>
		{:else}
			<p class="muted small">{t('assets.wf.selectItem')}</p>
		{/if}
	{/if}
	</Card>
</div>

{#if $assetsQuery.isLoading}
	<div class="grid">
		{#each [0, 1, 2] as n (n)}
			<div class="entity-card">
				<Skeleton height="225px" />
				<div class="skel-body">
					<Skeleton width="55%" height="15px" />
					<Skeleton width="85%" />
					<Skeleton width="70%" />
				</div>
			</div>
		{/each}
	</div>
{:else if $assetsQuery.data}
	{#if tab === 'characters'}
		{#if $assetsQuery.data.characters.length === 0}
			<EmptyState
				title={t('assets.noCharsTitle')}
				body={t('assets.noCharsBody')}
			>
				{#snippet icon()}
					<Icon name="assets" size={28} />
				{/snippet}
				{#snippet action()}
					<Button variant="primary" onclick={goToStory}>{t('assets.goToStory')}</Button>
				{/snippet}
			</EmptyState>
		{:else}
			<div class="grid">
				{#each $assetsQuery.data.characters as char (char.id)}
					{@const key = charKey(char.id)}
					{@const job = entityJob(jobs, 'character_id', char.id)}
					{@const jstate = jobStateOf(job)}
					<article class="entity-card">
						<div class="media">
							<AssetThumb
								src={assetUrl(char.sheet_path)}
								alt={t('assets.charSheetAlt', { name: char.name })}
								placeholder={t('assets.charSheetPlaceholder')}
								tall
								jobState={jstate}
								onPreview={(url) => openPreview(url, t('assets.charSheetAlt', { name: char.name }))}
							/>
							{#if isCover(char.sheet_path) && jstate !== 'generating'}
								<span class="cover-chip"><Icon name="image" size={11} /> {t('assets.cover')}</span>
							{/if}
							<div class="quick-actions">
								<Button
									variant="secondary"
									size="sm"
									title={t('assets.viewFull')}
									disabled={!char.sheet_path}
									onclick={() =>
										openPreview(assetUrl(char.sheet_path), t('assets.charSheetAlt', { name: char.name }))}
								>
									<Icon name="zoom-in" size={14} /><span class="sr-only"
										>{t('assets.viewCharFull', { name: char.name })}</span
									>
								</Button>
								<Button
									variant="secondary"
									size="sm"
									title={t('assets.regenSheet')}
									disabled={busy || sheetWorkflowId === '' || jstate === 'generating'}
									onclick={() => generateCharacter(char)}
								>
									<Icon name="retry" size={14} /><span class="sr-only"
										>{t('assets.regenSheetFor', { name: char.name })}</span
									>
								</Button>
								<Button
									variant="secondary"
									size="sm"
									title={isCover(char.sheet_path) ? t('assets.removeCover') : t('assets.setCover')}
									disabled={!char.sheet_path}
									onclick={() => toggleCover(char.sheet_path)}
								>
									<Icon name="image" size={14} /><span class="sr-only"
										>{isCover(char.sheet_path)
											? t('assets.removeCoverChar', { name: char.name })
											: t('assets.setCoverChar', { name: char.name })}</span
									>
								</Button>
								<Button
									variant="secondary"
									size="sm"
									title={t('common.edit')}
									onclick={() => openEditCharacter(char)}
								>
									<Icon name="edit" size={14} /><span class="sr-only">{t('common.edit')} {char.name}</span>
								</Button>
								<Button
									variant="secondary"
									size="sm"
									title={t('assets.deleteCharacter')}
									onclick={() =>
										requestDelete({ kind: 'character', id: char.id, name: char.name })}
								>
									<Icon name="trash" size={14} /><span class="sr-only"
										>{t('assets.deleteFor', { name: char.name })}</span
									>
								</Button>
							</div>
							{#if jstate === 'generating'}
								<div class="gen-bar">
									<ProgressBar indeterminate size="sm" label={t('assets.generating')} />
								</div>
							{/if}
						</div>

						<div class="entity-body">
							<div class="title-row">
								<strong>{char.name}</strong>
								{#if char.role}
									<span class="muted small"
										>{char.role}{#if char.age} · {char.age}{/if}</span
									>
								{:else if char.age}
									<span class="muted small">{char.age}</span>
								{/if}
							</div>
							{#if char.appearance}
								<p class="facts" title={char.appearance}>
									<span class="k">{t('assets.appearance')}</span> {char.appearance}
								</p>
							{/if}
							{#if char.personality}
								<p class="facts" title={char.personality}>
									<span class="k">{t('assets.personality')}</span> {char.personality}
								</p>
							{/if}
							{#if jstate === 'failed' && job}
								{#if job.error}
									<p class="err-line" title={job.error}>{job.error}</p>
								{/if}
							{/if}

							<div class="row">
								<Button
									variant="primary"
									size="sm"
									disabled={busy || sheetWorkflowId === '' || jstate === 'generating'}
									onclick={() => generateCharacter(char)}
								>
									{jstate === 'generating'
										? t('assets.generatingEllipsis')
										: char.sheet_path
											? t('assets.regenSheet')
											: t('assets.generateSheet')}
								</Button>
								<Button
									variant="secondary"
									size="sm"
									disabled={jstate === 'generating' || uploadingKey === charKey(char.id)}
									loading={uploadingKey === charKey(char.id)}
									onclick={() =>
										openOwnImage({ kind: 'character', id: char.id, name: char.name })}
								>
									<Icon name="upload" size={13} />
									{char.sheet_path ? t('assets.replaceImage') : t('assets.uploadImage')}
								</Button>
								{#if jstate === 'failed' && job}
									<Button
										variant="danger"
										size="sm"
										loading={$retryJobMutation.isPending}
										onclick={() => $retryJobMutation.mutate(job.id)}
									>
										<Icon name="retry" size={13} /> {t('common.retry')}
									</Button>
								{/if}
							</div>

							{#if showCharPrompt}
								<details class="prompt-fold">
									<summary>{t('assets.editPrompt')}</summary>
									<label class="prompt-field">
										<span class="field-label">{t('assets.sentToComfy')}</span>
										<textarea
											class="field-textarea prompt-area"
											rows="8"
											value={promptForChar(char)}
											oninput={(e) => setDraft(key, e.currentTarget.value)}
										></textarea>
									</label>
									<div class="row">
										<Button
											variant="ghost"
											size="sm"
											onclick={() => setDraft(key, characterSheetTemplate(char))}
										>
											{t('assets.resetSheetTemplate')}
										</Button>
										<Button
											variant="secondary"
											size="sm"
											loading={savingKey === key}
											onclick={() => saveCharacterPrompt(char)}
										>
											{t('assets.savePrompt')}
										</Button>
									</div>
								</details>
							{:else}
								<p class="muted small prompt-note">
		{t('assets.wf.noPromptPre')} <code>(Input:prompt)</code>{t('assets.wf.noPromptPost')}
	</p>
							{/if}
						</div>
					</article>
				{/each}
			</div>
		{/if}
	{:else if tab === 'locations'}
		{#if $assetsQuery.data.locations.length === 0}
			<EmptyState
				title={t('assets.noEnvsTitle')}
				body={t('assets.noEnvsBody')}
			>
				{#snippet icon()}
					<Icon name="folder" size={28} />
				{/snippet}
				{#snippet action()}
					<Button variant="primary" onclick={goToStory}>{t('assets.goToStory')}</Button>
				{/snippet}
			</EmptyState>
		{:else}
			<div class="grid">
				{#each $assetsQuery.data.locations as loc (loc.id)}
					{@const key = locKey(loc.id)}
					{@const job = entityJob(jobs, 'location_id', loc.id)}
					{@const jstate = jobStateOf(job)}
					<article class="entity-card">
						<div class="media">
							<AssetThumb
								src={assetUrl(loc.reference_image_path)}
								alt={loc.name}
								placeholder={t('assets.refPlaceholder')}
								tall
								jobState={jstate}
								onPreview={(url) => openPreview(url, loc.name)}
							/>
							{#if isCover(loc.reference_image_path) && jstate !== 'generating'}
								<span class="cover-chip"><Icon name="image" size={11} /> {t('assets.cover')}</span>
							{/if}
							<div class="quick-actions">
								<Button
									variant="secondary"
									size="sm"
									title={t('assets.viewFull')}
									disabled={!loc.reference_image_path}
									onclick={() => openPreview(assetUrl(loc.reference_image_path), loc.name)}
								>
									<Icon name="zoom-in" size={14} /><span class="sr-only"
										>{t('assets.viewRefFull', { name: loc.name })}</span
									>
								</Button>
								<Button
									variant="secondary"
									size="sm"
									title={t('assets.regenImage')}
									disabled={busy || envWorkflowId === '' || jstate === 'generating'}
									onclick={() => generateLocation(loc)}
								>
									<Icon name="retry" size={14} /><span class="sr-only"
										>{t('assets.regenImageFor', { name: loc.name })}</span
									>
								</Button>
								<Button
									variant="secondary"
									size="sm"
									title={isCover(loc.reference_image_path) ? t('assets.removeCover') : t('assets.setCover')}
									disabled={!loc.reference_image_path}
									onclick={() => toggleCover(loc.reference_image_path)}
								>
									<Icon name="image" size={14} /><span class="sr-only"
										>{isCover(loc.reference_image_path)
											? t('assets.removeCoverRef', { name: loc.name })
											: t('assets.setCoverRef', { name: loc.name })}</span
									>
								</Button>
								<Button
									variant="secondary"
									size="sm"
									title={t('common.edit')}
									onclick={() => openEditLocation(loc)}
								>
									<Icon name="edit" size={14} /><span class="sr-only">{t('common.edit')} {loc.name}</span>
								</Button>
								<Button
									variant="secondary"
									size="sm"
									title={t('assets.deleteEnvironment')}
									onclick={() =>
										requestDelete({ kind: 'location', id: loc.id, name: loc.name })}
								>
									<Icon name="trash" size={14} /><span class="sr-only">{t('assets.deleteFor', { name: loc.name })}</span>
								</Button>
							</div>
							{#if jstate === 'generating'}
								<div class="gen-bar">
									<ProgressBar indeterminate size="sm" label={t('assets.generating')} />
								</div>
							{/if}
						</div>

						<div class="entity-body">
							<div class="title-row">
								<strong>{loc.name}</strong>
							</div>
							{#if loc.description}
								<p class="facts" title={loc.description}>
									<span class="k">{t('assets.description')}</span> {loc.description}
								</p>
							{/if}
							{#if jstate === 'failed' && job}
								{#if job.error}
									<p class="err-line" title={job.error}>{job.error}</p>
								{/if}
							{/if}

							<div class="row">
								<Button
									variant="primary"
									size="sm"
									disabled={busy || envWorkflowId === '' || jstate === 'generating'}
									onclick={() => generateLocation(loc)}
								>
									{jstate === 'generating'
										? t('assets.generatingEllipsis')
										: loc.reference_image_path
											? t('assets.regenImage')
											: t('assets.generateImage')}
								</Button>
								<Button
									variant="secondary"
									size="sm"
									disabled={jstate === 'generating' || uploadingKey === locKey(loc.id)}
									loading={uploadingKey === locKey(loc.id)}
									onclick={() =>
										openOwnImage({ kind: 'location', id: loc.id, name: loc.name })}
								>
									<Icon name="upload" size={13} />
									{loc.reference_image_path ? t('assets.replaceImage') : t('assets.uploadImage')}
								</Button>
								{#if jstate === 'failed' && job}
									<Button
										variant="danger"
										size="sm"
										loading={$retryJobMutation.isPending}
										onclick={() => $retryJobMutation.mutate(job.id)}
									>
										<Icon name="retry" size={13} /> {t('common.retry')}
									</Button>
								{/if}
							</div>

							{#if envHasPrompt || routerModelOf(envWorkflowId)}
								<details class="prompt-fold">
									<summary>{t('assets.editPrompt')}</summary>
									<label class="prompt-field">
										<span class="field-label">{t('assets.sentToComfy')}</span>
										<textarea
											class="field-textarea prompt-area"
											rows="7"
											value={promptForLoc(loc)}
											oninput={(e) => setDraft(key, e.currentTarget.value)}
										></textarea>
									</label>
									<div class="row">
										<Button
											variant="ghost"
											size="sm"
											onclick={() => setDraft(key, locationReferenceTemplate(loc))}
										>
											{t('assets.resetEnvTemplate')}
										</Button>
										<Button
											variant="secondary"
											size="sm"
											loading={savingKey === key}
											onclick={() => saveLocationPrompt(loc)}
										>
											{t('assets.savePrompt')}
										</Button>
									</div>
								</details>
							{:else}
								<p class="muted small prompt-note">
		{t('assets.wf.noPromptPre')} <code>(Input:prompt)</code>{t('assets.wf.noPromptPost')}
	</p>
							{/if}
						</div>
					</article>
				{/each}
			</div>
		{/if}
	{:else}
		{#if $assetsQuery.data.items.length === 0}
			<EmptyState
				title={t('assets.noItemsTitle')}
				body={t('assets.noItemsBody')}
			>
				{#snippet icon()}
					<Icon name="folder" size={28} />
				{/snippet}
				{#snippet action()}
					<Button variant="primary" onclick={goToStory}>{t('assets.goToStory')}</Button>
				{/snippet}
			</EmptyState>
		{:else}
			<div class="grid">
				{#each $assetsQuery.data.items as item (item.id)}
					{@const key = itemKey(item.id)}
					{@const job = entityJob(jobs, 'item_id', item.id)}
					{@const jstate = jobStateOf(job)}
					<article class="entity-card">
						<div class="media">
							<AssetThumb
								src={assetUrl(item.reference_image_path)}
								alt={item.name}
								placeholder={t('assets.refPlaceholder')}
								tall
								jobState={jstate}
								onPreview={(url) => openPreview(url, item.name)}
							/>
							{#if isCover(item.reference_image_path) && jstate !== 'generating'}
								<span class="cover-chip"><Icon name="image" size={11} /> {t('assets.cover')}</span>
							{/if}
							<div class="quick-actions">
								<Button
									variant="secondary"
									size="sm"
									title={t('assets.viewFull')}
									disabled={!item.reference_image_path}
									onclick={() => openPreview(assetUrl(item.reference_image_path), item.name)}
								>
									<Icon name="zoom-in" size={14} /><span class="sr-only"
										>{t('assets.viewRefFull', { name: item.name })}</span
									>
								</Button>
								<Button
									variant="secondary"
									size="sm"
									title={t('assets.regenImage')}
									disabled={busy || itemWorkflowId === '' || jstate === 'generating'}
									onclick={() => generateItem(item)}
								>
									<Icon name="retry" size={14} /><span class="sr-only"
										>{t('assets.regenImageFor', { name: item.name })}</span
									>
								</Button>
								<Button
									variant="secondary"
									size="sm"
									title={isCover(item.reference_image_path) ? t('assets.removeCover') : t('assets.setCover')}
									disabled={!item.reference_image_path}
									onclick={() => toggleCover(item.reference_image_path)}
								>
									<Icon name="image" size={14} /><span class="sr-only"
										>{isCover(item.reference_image_path)
											? t('assets.removeCoverRef', { name: item.name })
											: t('assets.setCoverRef', { name: item.name })}</span
									>
								</Button>
								<Button
									variant="secondary"
									size="sm"
									title={t('common.edit')}
									onclick={() => openEditItem(item)}
								>
									<Icon name="edit" size={14} /><span class="sr-only">{t('common.edit')} {item.name}</span>
								</Button>
								<Button
									variant="secondary"
									size="sm"
									title={t('assets.deleteItem')}
									onclick={() =>
										requestDelete({ kind: 'item', id: item.id, name: item.name })}
								>
									<Icon name="trash" size={14} /><span class="sr-only">{t('assets.deleteFor', { name: item.name })}</span>
								</Button>
							</div>
							{#if jstate === 'generating'}
								<div class="gen-bar">
									<ProgressBar indeterminate size="sm" label={t('assets.generating')} />
								</div>
							{/if}
						</div>

						<div class="entity-body">
							<div class="title-row">
								<strong>{item.name}</strong>
							</div>
							{#if item.description}
								<p class="facts" title={item.description}>
									<span class="k">{t('assets.description')}</span> {item.description}
								</p>
							{/if}
							{#if jstate === 'failed' && job}
								{#if job.error}
									<p class="err-line" title={job.error}>{job.error}</p>
								{/if}
							{/if}

							<div class="row">
								<Button
									variant="primary"
									size="sm"
									disabled={busy || itemWorkflowId === '' || jstate === 'generating'}
									onclick={() => generateItem(item)}
								>
									{jstate === 'generating'
										? t('assets.generatingEllipsis')
										: item.reference_image_path
											? t('assets.regenImage')
											: t('assets.generateImage')}
								</Button>
								<Button
									variant="secondary"
									size="sm"
									disabled={jstate === 'generating' || uploadingKey === itemKey(item.id)}
									loading={uploadingKey === itemKey(item.id)}
									onclick={() =>
										openOwnImage({ kind: 'item', id: item.id, name: item.name })}
								>
									<Icon name="upload" size={13} />
									{item.reference_image_path ? t('assets.replaceImage') : t('assets.uploadImage')}
								</Button>
								{#if jstate === 'failed' && job}
									<Button
										variant="danger"
										size="sm"
										loading={$retryJobMutation.isPending}
										onclick={() => $retryJobMutation.mutate(job.id)}
									>
										<Icon name="retry" size={13} /> {t('common.retry')}
									</Button>
								{/if}
							</div>

							{#if itemHasPrompt || routerModelOf(itemWorkflowId)}
								<details class="prompt-fold">
									<summary>{t('assets.editPrompt')}</summary>
									<label class="prompt-field">
										<span class="field-label">{t('assets.sentToComfy')}</span>
										<textarea
											class="field-textarea prompt-area"
											rows="7"
											value={promptForItem(item)}
											oninput={(e) => setDraft(key, e.currentTarget.value)}
										></textarea>
									</label>
									<div class="row">
										<Button
											variant="ghost"
											size="sm"
											onclick={() => setDraft(key, itemReferenceTemplate(item))}
										>
											{t('assets.resetItemTemplate')}
										</Button>
										<Button
											variant="secondary"
											size="sm"
											loading={savingKey === key}
											onclick={() => saveItemPrompt(item)}
										>
											{t('assets.savePrompt')}
										</Button>
									</div>
								</details>
							{:else}
								<p class="muted small prompt-note">
		{t('assets.wf.noPromptPre')} <code>(Input:prompt)</code>{t('assets.wf.noPromptPost')}
	</p>
							{/if}
						</div>
					</article>
				{/each}
			</div>
		{/if}
	{/if}
{/if}

<ImageLightbox
	src={previewSrc}
	alt={previewAlt}
	onClose={() => {
		previewSrc = null;
		previewAlt = '';
	}}
/>

<Modal
	bind:open={createAssetOpen}
	title={t('assets.addTitle', { kind: kindNoun(createAssetKind) })}
	resizable
	closeOnBackdrop={false}
	onclose={() => {
		createAssetName = '';
		createAssetDescription = '';
		createAssetRole = '';
		createAssetAge = '';
		createAssetPersonality = '';
	}}
>
	<form
		onsubmit={(event) => {
			event.preventDefault();
			createAsset();
		}}
	>
		<label class="field">
			<span class="field-label">{t('common.name')}</span>
			<input class="field-input" bind:value={createAssetName} required />
		</label>
		{#if createAssetKind === 'character'}
			<div class="create-character-details">
				<label class="field">
					<span class="field-label">{t('assets.role')}</span>
					<input class="field-input" bind:value={createAssetRole} />
				</label>
				<label class="field">
					<span class="field-label">{t('assets.age')}</span>
					<input
						class="field-input"
						maxlength="15"
						bind:value={createAssetAge}
					/>
				</label>
			</div>
			<label class="field">
				<span class="field-label">{t('assets.appearance')}</span>
				<textarea class="field-textarea" rows="3" bind:value={createAssetDescription}></textarea>
			</label>
			<label class="field">
				<span class="field-label">{t('assets.personality')}</span>
				<textarea class="field-textarea" rows="3" bind:value={createAssetPersonality}></textarea>
			</label>
		{:else}
			<label class="field">
				<span class="field-label">{t('assets.description')}</span>
				<textarea class="field-textarea" rows="3" bind:value={createAssetDescription}></textarea>
			</label>
		{/if}
		<div class="field">
			<span class="field-label">{t('assets.addImage')}</span>
			<div class="create-image-actions">
				<Button
					variant="secondary"
					size="sm"
					loading={uploadingCreateAssetImage}
					disabled={$createEntityMutation.isPending}
					onclick={() => createAssetFileInput?.click()}
				>
					<Icon name="upload" size={13} /> {t('assets.uploadImage')}
				</Button>
				{#if createAssetImagePath}
					<Button
						variant="ghost"
						size="sm"
						disabled={$createEntityMutation.isPending}
						onclick={() => (createAssetImagePath = null)}
					>
						{t('assets.removeSelectedImage')}
					</Button>
				{/if}
			</div>
			{#if $libraryMediaQuery.isLoading}
				<p class="muted small">{t('common.loading')}</p>
			{:else if $libraryMediaQuery.isError}
				<p class="err-line">
					{$libraryMediaQuery.error instanceof Error
						? $libraryMediaQuery.error.message
						: t('assets.libraryLoadFailed')}
				</p>
			{:else if ($libraryMediaQuery.data ?? []).filter((media) => media.kind === 'image').length > 0}
				<p class="field-label">{t('assets.chooseLibraryImage')}</p>
				<div class="create-image-grid" aria-label={t('assets.chooseLibraryImage')}>
					{#each ($libraryMediaQuery.data ?? []).filter((media) => media.kind === 'image') as media (media.path)}
						{@const src = assetUrl(media.path)}
						<button
							type="button"
							class="create-image-option"
							class:selected={createAssetImagePath === media.path}
							aria-pressed={createAssetImagePath === media.path}
							title={media.name}
							disabled={$createEntityMutation.isPending}
							onclick={() => (createAssetImagePath = media.path)}
						>
							{#if src}<img src={src} alt="" loading="lazy" />{/if}
							<span>{media.name}</span>
						</button>
					{/each}
				</div>
			{:else}
				<p class="muted small">{t('assets.noLibraryImages')}</p>
			{/if}
		</div>
	</form>
	{#snippet footer()}
		<Button variant="ghost" onclick={() => (createAssetOpen = false)}>{t('common.cancel')}</Button>
		<Button
			variant="primary"
			loading={$createEntityMutation.isPending}
			disabled={!createAssetName.trim() || uploadingCreateAssetImage}
			onclick={createAsset}
		>
			{t('assets.addAsset')}
		</Button>
	{/snippet}
</Modal>

<Modal
	bind:open={editAssetOpen}
	title={t('assets.editTitle', { kind: kindNoun(editAssetKind) })}
	resizable
	closeOnBackdrop={false}
	onclose={() => {
		editAssetId = null;
		editAssetImageChanged = false;
	}}
>
	<form
		onsubmit={(event) => {
			event.preventDefault();
			saveEditedAsset();
		}}
	>
		<label class="field">
			<span class="field-label">{t('common.name')}</span>
			<input
				class="field-input"
				bind:value={editAssetName}
				required
				disabled={$updateEntityMutation.isPending}
			/>
		</label>
		{#if editAssetKind === 'character'}
			<div class="create-character-details">
				<label class="field">
					<span class="field-label">{t('assets.role')}</span>
					<input class="field-input" bind:value={editAssetRole} disabled={$updateEntityMutation.isPending} />
				</label>
				<label class="field">
					<span class="field-label">{t('assets.age')}</span>
					<input
						class="field-input"
						maxlength="15"
						bind:value={editAssetAge}
						disabled={$updateEntityMutation.isPending}
					/>
				</label>
			</div>
			<label class="field">
				<span class="field-label">{t('assets.appearance')}</span>
				<textarea
					class="field-textarea"
					rows="3"
					bind:value={editAssetDescription}
					disabled={$updateEntityMutation.isPending}
				></textarea>
			</label>
			<label class="field">
				<span class="field-label">{t('assets.personality')}</span>
				<textarea
					class="field-textarea"
					rows="3"
					bind:value={editAssetPersonality}
					disabled={$updateEntityMutation.isPending}
				></textarea>
			</label>
		{:else}
			<label class="field">
				<span class="field-label">{t('assets.description')}</span>
				<textarea
					class="field-textarea"
					rows="3"
					bind:value={editAssetDescription}
					disabled={$updateEntityMutation.isPending}
				></textarea>
			</label>
		{/if}
		<div class="field">
			<span class="field-label">{t('assets.addImage')}</span>
			<div class="create-image-actions">
				<Button
					variant="secondary"
					size="sm"
					loading={uploadingCreateAssetImage}
					disabled={$updateEntityMutation.isPending}
					onclick={() => createAssetFileInput?.click()}
				>
					<Icon name="upload" size={13} /> {t('assets.uploadImage')}
				</Button>
				{#if editAssetImagePath}
					<Button
						variant="ghost"
						size="sm"
						disabled={$updateEntityMutation.isPending}
						onclick={() => {
							editAssetImagePath = null;
							editAssetImageChanged = true;
						}}
					>
						{t('assets.removeSelectedImage')}
					</Button>
				{/if}
			</div>
			{#if $libraryMediaQuery.isLoading}
				<p class="muted small">{t('common.loading')}</p>
			{:else if $libraryMediaQuery.isError}
				<p class="err-line">
					{$libraryMediaQuery.error instanceof Error
						? $libraryMediaQuery.error.message
						: t('assets.libraryLoadFailed')}
				</p>
			{:else if ($libraryMediaQuery.data ?? []).filter((media) => media.kind === 'image').length > 0}
				<p class="field-label">{t('assets.chooseLibraryImage')}</p>
				<div class="create-image-grid" aria-label={t('assets.chooseLibraryImage')}>
					{#each ($libraryMediaQuery.data ?? []).filter((media) => media.kind === 'image') as media (media.path)}
						{@const src = assetUrl(media.path)}
						<button
							type="button"
							class="create-image-option"
							class:selected={editAssetImagePath === media.path}
							aria-pressed={editAssetImagePath === media.path}
							title={media.name}
							disabled={$updateEntityMutation.isPending}
							onclick={() => {
								editAssetImagePath = media.path;
								editAssetImageChanged = true;
							}}
						>
							{#if src}<img src={src} alt="" loading="lazy" />{/if}
							<span>{media.name}</span>
						</button>
					{/each}
				</div>
			{:else}
				<p class="muted small">{t('assets.noLibraryImages')}</p>
			{/if}
		</div>
	</form>
	{#snippet footer()}
		<Button variant="ghost" onclick={() => (editAssetOpen = false)}>{t('common.cancel')}</Button>
		<Button
			variant="primary"
			loading={$updateEntityMutation.isPending}
			disabled={!editAssetName.trim() || uploadingCreateAssetImage}
			onclick={saveEditedAsset}
		>
			{t('common.save')}
		</Button>
	{/snippet}
</Modal>

<ConfirmDialog
	bind:open={deleteOpen}
	title={deleteTarget ? t('assets.confirmDeleteTitle', { kind: kindNoun(deleteTarget.kind) }) : t('assets.confirmDeleteFallback')}
	message={deleteTarget
		? t('assets.confirmDeleteBody', { kind: kindNoun(deleteTarget.kind), name: deleteTarget.name })
		: ''}
	confirmLabel={t('assets.confirmDeleteLabel')}
	danger
	onconfirm={() => {
		const target = deleteTarget;
		deleteTarget = null;
		if (target) $deleteEntityMutation.mutate(target);
	}}
	oncancel={() => (deleteTarget = null)}
/>

<style>
	.stage-header {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		margin-bottom: var(--space-md);
		gap: var(--space-md);
	}
	.stage-header h2 {
		margin: 0 0 6px;
		font-size: 22px;
		font-weight: 700;
	}
	.lead {
		margin: 0;
		color: var(--text-secondary);
		font-size: 13px;
		max-width: 52ch;
		line-height: 1.45;
	}
	.stage-actions {
		display: flex;
		gap: var(--space-sm);
		flex-shrink: 0;
	}
	.muted {
		color: var(--text-secondary);
		margin: 0;
	}
	.small {
		font-size: 12px;
	}
	.tabs-row {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--space-md);
		margin-bottom: var(--space-md);
	}
	.tabs {
		display: flex;
		gap: 8px;
	}
	.tabs button {
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		color: var(--text-secondary);
		border-radius: var(--radius-sm);
		padding: 8px 14px;
		cursor: pointer;
		min-height: 36px;
	}
	.tabs button.active {
		background: var(--accent);
		border-color: var(--accent);
		color: white;
	}
	.tabs button:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.hint-warn {
		margin: 0 0 var(--space-md);
		font-size: 13px;
		color: var(--warning);
	}
	.hint-warn a {
		color: var(--accent);
	}
	.settings-wrap {
		margin-bottom: var(--space-lg);
	}
	.card-h {
		margin: 0;
		font-size: 15px;
		font-weight: 600;
		color: var(--text-primary);
	}
	.wf-field {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin-bottom: var(--space-md);
	}
	.wf-field .field-select {
		width: 100%;
		max-width: 100%;
	}
	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
		gap: var(--space-md);
	}
	.entity-card {
		background: var(--bg-surface);
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
		overflow: hidden;
		display: flex;
		flex-direction: column;
		min-width: 0;
	}
	.skel-body {
		display: flex;
		flex-direction: column;
		gap: 10px;
		padding: 14px;
	}
	.media {
		position: relative;
	}
	.media :global(.thumb) {
		border: none;
		border-radius: 0;
		border-bottom: 1px solid var(--border);
	}
	.quick-actions {
		position: absolute;
		top: 8px;
		right: 8px;
		display: flex;
		gap: 6px;
		opacity: 0;
		transform: translateY(-4px);
		transition:
			opacity 150ms ease,
			transform 150ms ease;
	}
	.media:hover .quick-actions,
	.media:focus-within .quick-actions {
		opacity: 1;
		transform: none;
	}
	@media (hover: none) {
		.quick-actions {
			opacity: 1;
			transform: none;
		}
	}
	.cover-chip {
		position: absolute;
		left: 8px;
		bottom: 8px;
		z-index: 1;
		display: inline-flex;
		align-items: center;
		gap: 5px;
		padding: 3px 9px;
		border-radius: 999px;
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		line-height: 1.4;
		color: var(--accent);
		background: color-mix(in srgb, var(--accent) 22%, rgba(5, 5, 8, 0.72));
		border: 1px solid color-mix(in srgb, var(--accent) 45%, transparent);
		filter: drop-shadow(0 1px 3px rgba(0, 0, 0, 0.6));
	}
	.gen-bar {
		position: absolute;
		left: 0;
		right: 0;
		bottom: 0;
		padding: 10px 12px 12px;
		background: linear-gradient(transparent, rgba(5, 5, 8, 0.85));
	}
	.entity-body {
		padding: 12px 14px 14px;
		display: flex;
		flex-direction: column;
		gap: 8px;
		min-width: 0;
	}
	.title-row {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: 10px;
	}
	.facts {
		margin: 0;
		font-size: 12px;
		color: var(--text-secondary);
		line-height: 1.4;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}
	.facts .k {
		color: var(--text-muted);
		font-size: 11px;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		margin-right: 6px;
	}
	.err-line {
		margin: 0;
		font-size: 12px;
		color: var(--error);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.create-character-details {
		display: grid;
		grid-template-columns: minmax(0, 1fr) 120px;
		gap: 12px;
	}
	.create-image-actions {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		margin-bottom: 10px;
	}
	.create-image-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(88px, 1fr));
		gap: 8px;
		max-height: 220px;
		overflow-y: auto;
		padding: 2px;
	}
	.create-image-option {
		display: flex;
		flex-direction: column;
		gap: 5px;
		min-width: 0;
		padding: 4px;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: var(--bg-elevated);
		color: var(--text-secondary);
		font: inherit;
		font-size: 11px;
		text-align: left;
		cursor: pointer;
	}
	.create-image-option:hover,
	.create-image-option.selected {
		border-color: var(--accent);
		color: var(--text-primary);
	}
	.create-image-option img {
		width: 100%;
		aspect-ratio: 1;
		object-fit: cover;
		border-radius: calc(var(--radius-sm) - 2px);
	}
	.create-image-option span {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.row {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 8px;
	}
	.prompt-note {
		margin: 0;
	}
	.prompt-fold {
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: var(--bg-elevated);
	}
	.prompt-fold summary {
		cursor: pointer;
		padding: 10px 12px;
		font-size: 13px;
		font-weight: 600;
		color: var(--text-secondary);
		list-style: none;
		user-select: none;
	}
	.prompt-fold summary::-webkit-details-marker {
		display: none;
	}
	.prompt-fold summary::before {
		content: '▸';
		display: inline-block;
		margin-right: 8px;
		color: var(--text-muted);
		transition: transform 0.15s;
	}
	.prompt-fold summary:hover {
		color: var(--text-primary);
	}
	.prompt-fold[open] summary::before {
		transform: rotate(90deg);
	}
	.prompt-fold[open] summary {
		border-bottom: 1px solid var(--border);
		color: var(--text-primary);
	}
	.prompt-fold .prompt-field {
		display: block;
		padding: 12px;
		margin: 0;
	}
	.prompt-fold .row {
		padding: 0 12px 12px;
	}
	.prompt-area {
		font-family: var(--font-mono);
		font-size: 13px;
	}
	.sr-only {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0, 0, 0, 0);
		white-space: nowrap;
		border: 0;
	}
</style>
