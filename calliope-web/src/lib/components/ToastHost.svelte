<script lang="ts">
	import { onMount } from 'svelte';
	import {
		dismissToast,
		pauseToast,
		resumeToast,
		subscribeToasts,
		type Toast,
	} from '$lib/toast';
	import Icon from '$lib/components/ui/Icon.svelte';
	import type { IconName } from '$lib/components/ui/icons';

	let items = $state<Toast[]>([]);

	onMount(() => subscribeToasts((next) => (items = next)));

	const kindIcon: Record<Toast['kind'], IconName> = {
		success: 'check',
		error: 'alert',
		info: 'info',
	};
</script>

{#if items.length}
	<div class="toast-host" aria-live="polite" aria-relevant="additions">
		{#each items as t (t.id)}
			<div
				class="toast {t.kind}"
				role="status"
				onmouseenter={() => pauseToast(t.id)}
				onmouseleave={() => resumeToast(t.id)}
				onfocusin={() => pauseToast(t.id)}
				onfocusout={() => resumeToast(t.id)}
			>
				<span class="badge" aria-hidden="true">
					<span class="dot"></span>
					<Icon name={kindIcon[t.kind]} size={15} />
				</span>
				<span class="msg">{t.message}</span>
				<button
					type="button"
					class="dismiss"
					aria-label="Dismiss"
					onclick={() => dismissToast(t.id)}
				>
					<Icon name="close" size={14} />
				</button>
			</div>
		{/each}
	</div>
{/if}

<style>
	.toast-host {
		position: fixed;
		top: 72px;
		right: 20px;
		z-index: 1000;
		display: flex;
		flex-direction: column;
		gap: 8px;
		max-width: min(360px, calc(100vw - 32px));
		pointer-events: none;
	}
	.toast {
		pointer-events: auto;
		display: flex;
		align-items: flex-start;
		gap: 10px;
		padding: 12px 14px;
		border-radius: var(--radius-md);
		border: 1px solid var(--border);
		background: var(--bg-surface);
		box-shadow: 0 12px 32px rgba(0, 0, 0, 0.45);
		animation: toast-in 0.22s ease-out;
	}
	.toast.success {
		border-color: rgba(34, 197, 94, 0.45);
		box-shadow:
			0 12px 32px rgba(0, 0, 0, 0.45),
			0 0 0 1px rgba(34, 197, 94, 0.12);
	}
	.toast.error {
		border-color: rgba(239, 68, 68, 0.5);
	}
	.toast.info {
		border-color: rgba(59, 130, 246, 0.45);
	}
	.badge {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		margin-top: 1px;
		flex-shrink: 0;
	}
	.dot {
		width: 7px;
		height: 7px;
		border-radius: 50%;
		background: currentColor;
	}
	.toast.success .badge {
		color: var(--success);
	}
	.toast.error .badge {
		color: var(--error);
	}
	.toast.info .badge {
		color: var(--info);
	}
	.msg {
		flex: 1;
		font-size: 13px;
		line-height: 1.4;
		color: var(--text-primary);
		font-weight: 500;
	}
	.dismiss {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		border: none;
		background: transparent;
		color: var(--text-muted);
		cursor: pointer;
		padding: 0 2px;
		min-width: 28px;
		min-height: 28px;
		border-radius: 4px;
	}
	.dismiss:hover {
		color: var(--text-primary);
	}
	.dismiss:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	@keyframes toast-in {
		from {
			opacity: 0;
			transform: translateY(-8px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.toast {
			animation: none;
		}
	}
	@media (max-width: 640px) {
		.toast-host {
			top: auto;
			bottom: 20px;
			right: 16px;
			left: 16px;
			max-width: none;
		}
	}
</style>
