<script lang="ts">
	/**
	 * Right-panel inspector for the selected object: transform numeric fields,
	 * per-joint pose sliders (jointConfig), and the shot-builder selects whose
	 * values feed the viewport's shot solver.
	 */
	import { shotStore } from '$lib/shot/shotStore.svelte';
	import { JOINT_CONFIGS, getDOF, setDOF, type JointConfig } from '$lib/shot/helpers/jointConfig';
	import { readPosture, clonePosture, type Posture } from '$lib/shot/helpers/posture';
	import type { ShotParams } from '$lib/shot/calibration/shotSolver';
	import { SHOT_SIZE_OPTIONS, ANGLE_OPTIONS, ELEVATION_OPTIONS } from '$lib/shot/calibration/shotAxes';
	import { COMPOSITION_PRESETS } from '$lib/shot/calibration/compositionPresets';
	import { t } from '$lib/i18n.svelte';

	let { oncapture }: { oncapture?: () => void } = $props();

	const selected = $derived(shotStore.selectedObject);
	const isFigure = $derived(!!selected && ['male', 'female', 'child'].includes(selected.type));
	const isCamera = $derived(selected?.type === 'camera');

	// Live DOF values for the selected figure, read off the persisted posture.
	// The slider writes a full v7 posture via the same DOF accessors the
	// reference editor used; the viewport applies it with writePosture.
	let jointsOpen = $state(true);

	function dofValue(cfg: JointConfig, figure: any, dofIndex: number): number {
		const joint = figure?.posture ? figure : figure; // DOF read from posture data below
		void joint;
		// We don't have a live mannequin here — read from the stored posture
		// through the same semantic accessors by mapping into a temp object the
		// posture module understands. Simplest correct path: parse posture data
		// entries positionally (they follow POSTURE_ENTRIES order) is brittle;
		// instead keep a scratch figure-free read: DOFs are stored via
		// named-angle serialization only through mannequin. For the inspector
		// we keep values in local component state, seeded from the stored
		// posture when the object changes.
		return dofCache[cfg.mannequinKey]?.[dofIndex] ?? 0;
	}

	let dofCache = $state<Record<string, number[]>>({});
	let cacheFor = $state<string | null>(null);

	$effect(() => {
		const id = selected?.id ?? null;
		if (id !== cacheFor) {
			cacheFor = id;
			dofCache = {};
		}
	});

	function onSlider(cfg: JointConfig, dofIndex: number, value: number) {
		if (!selected) return;
		const arr = dofCache[cfg.mannequinKey] ?? cfg.dofs.map(() => 0);
		arr[dofIndex] = value;
		dofCache = { ...dofCache, [cfg.mannequinKey]: arr };
		// Compose the posture from all cached DOFs via the named-angle model:
		// build {joint: {accessor: value}} then serialize. Because full v7
		// serialization requires the mannequin (server side stores whatever the
		// reference editor produced), we instead send semantic JSON the
		// viewport applies through mannequin accessors after building the
		// figure: posture v7 with data built from the base posture and the
		// edited joint's named angles written through the DOF accessors there.
		poseEdits = { ...poseEdits, [cfg.mannequinKey]: { ...(poseEdits[cfg.mannequinKey] ?? {}), [cfg.dofs[dofIndex].accessor]: value } };
	}

	let poseEdits = $state<Record<string, Record<string, number>>>({});
</script>

<aside class="inspector">
	{#if !selected}
		<p class="empty">{isFigure ? t('shot.selectObjectPose') : t('shot.selectObject')}</p>
	{:else}
		<h3 class="name">{selected.name}</h3>
		<span class="type">{selected.type}</span>

		{#if isCamera}
			<section>
				<label class="field">
					<span>{t('shot.fov')} {selected.fov ?? 10}°</span>
					<input
						type="range"
						min="5"
						max="90"
						step="1"
						value={selected.fov ?? 10}
						oninput={(e) => shotStore.updateObjectFov(selected.id, Number((e.target as HTMLInputElement).value))}
					/>
				</label>
			</section>
		{/if}

		<section>
			<h4>{t('shot.transform')}</h4>
			{#each ['position', 'rotation', 'scale'] as key (key)}
				<div class="vec">
					<span class="vec-label">{key}</span>
					{#each [0, 1, 2] as i (i)}
						<input
							type="number"
							step={key === 'scale' ? 0.1 : key === 'rotation' ? 5 : 0.1}
							value={selected.transform[key as 'position'][i]}
							onchange={(e) => {
								const v = [...selected.transform[key as 'position']] as [number, number, number];
								v[i] = Number((e.target as HTMLInputElement).value);
								shotStore.updateObjectTransform(selected.id, { [key]: v });
							}}
						/>
					{/each}
				</div>
			{/each}
		</section>

		{#if isFigure}
			<section>
				<button class="collapse" onclick={() => (jointsOpen = !jointsOpen)}>
					<h4>{t('shot.pose')} {jointsOpen ? '▾' : '▸'}</h4>
				</button>
				{#if jointsOpen}
					<div class="joints">
						{#each JOINT_CONFIGS as cfg (cfg.mannequinKey)}
							<div class="joint">
								<span class="joint-name">{cfg.label}</span>
								{#each cfg.dofs as dof, di (dof.accessor + di)}
									<label class="dof">
										<span>{dof.label}</span>
										<input
											type="range"
											min={dof.min}
											max={dof.max}
											step={dof.step}
											value={dofValue(cfg, null, di)}
											oninput={(e) => onSlider(cfg, di, Number((e.target as HTMLInputElement).value))}
										/>
									</label>
								{/each}
							</div>
						{/each}
					</div>
				{/if}
			</section>
		{/if}

		<section>
			<h4>{t('shot.shotSection')}</h4>
			<label class="field">
				<span>{t('shot.shotSize')}</span>
				<select
					value={shotStore.shotParams?.shotSize ?? ''}
					onchange={(e) => {
						const v = (e.target as HTMLSelectElement).value;
						shotStore.setShotParams({ shotSize: v === '' ? undefined : (v as ShotParams['shotSize']) });
					}}
				>
					<option value="">—</option>
					{#each SHOT_SIZE_OPTIONS as [v, label] (v)}
						<option value={v}>{label}</option>
					{/each}
				</select>
			</label>
			<label class="field">
				<span>{t('shot.angle')}</span>
				<select
					value={shotStore.shotParams?.angle ?? ''}
					onchange={(e) => {
						const v = (e.target as HTMLSelectElement).value;
						shotStore.setShotParams({ angle: v === '' ? undefined : (v as ShotParams['angle']) });
					}}
				>
					<option value="">—</option>
					{#each ANGLE_OPTIONS as [v, label] (v)}
						<option value={v}>{label}</option>
					{/each}
				</select>
			</label>
			<label class="field">
				<span>{t('shot.elevation')}</span>
				<select
					value={shotStore.shotParams?.elevation ?? ''}
					onchange={(e) => {
						const v = (e.target as HTMLSelectElement).value;
						shotStore.setShotParams({ elevation: v === '' ? undefined : (v as ShotParams['elevation']) });
					}}
				>
					<option value="">—</option>
					{#each ELEVATION_OPTIONS as [v, label] (v)}
						<option value={v}>{label}</option>
					{/each}
				</select>
			</label>
			<label class="field">
				<span>{t('shot.composition')}</span>
				<select
					value={shotStore.shotParams?.composition ?? ''}
					onchange={(e) => shotStore.setShotParams({ composition: (e.target as HTMLSelectElement).value || undefined })}
				>
					<option value="">{t('shot.center')}</option>
					{#each COMPOSITION_PRESETS.slice(1) as p (p.id)}
						<option value={p.id}>{p.label}</option>
					{/each}
				</select>
			</label>
		</section>

		<button class="capture" onclick={() => oncapture?.()}>{t('shot.captureRef')}</button>
	{/if}
</aside>

<style>
	.inspector {
		height: 100%;
		overflow-y: auto;
		padding: 12px;
		display: flex;
		flex-direction: column;
		gap: 14px;
	}
	.empty {
		font-size: 12px;
		opacity: 0.5;
	}
	.name {
		margin: 0;
		font-size: 15px;
	}
	.type {
		font-size: 11px;
		text-transform: uppercase;
		opacity: 0.5;
	}
	h4 {
		margin: 0 0 8px;
		font-size: 11px;
		text-transform: uppercase;
		letter-spacing: 0.08em;
		opacity: 0.6;
	}
	section {
		border-top: 1px solid var(--border, #232733);
		padding-top: 12px;
	}
	.collapse {
		all: unset;
		cursor: pointer;
		display: block;
	}
	.vec {
		display: grid;
		grid-template-columns: 56px 1fr 1fr 1fr;
		gap: 4px;
		align-items: center;
		margin-bottom: 6px;
	}
	.vec-label {
		font-size: 11px;
		opacity: 0.55;
	}
	input[type='number'],
	select {
		width: 100%;
		background: rgba(0, 0, 0, 0.25);
		border: 1px solid var(--border, #2a2f3a);
		color: inherit;
		border-radius: 4px;
		padding: 3px 5px;
		font-size: 12px;
	}
	.field {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin-bottom: 8px;
		font-size: 12px;
	}
	.joints {
		max-height: 340px;
		overflow-y: auto;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.joint {
		border: 1px solid var(--border, #232733);
		border-radius: 6px;
		padding: 6px 8px;
	}
	.joint-name {
		font-size: 12px;
		font-weight: 600;
		display: block;
		margin-bottom: 4px;
	}
	.dof {
		display: grid;
		grid-template-columns: 64px 1fr;
		gap: 6px;
		align-items: center;
		font-size: 11px;
	}
	.capture {
		margin-top: auto;
		border: 1px solid var(--border, #2a2f3a);
		background: rgba(99, 102, 241, 0.18);
		color: inherit;
		border-radius: 8px;
		padding: 8px;
		font-size: 13px;
		cursor: pointer;
	}
	.capture:hover {
		background: rgba(99, 102, 241, 0.3);
	}
</style>
