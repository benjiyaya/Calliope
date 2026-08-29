<script lang="ts">
	import type { Snippet } from 'svelte';
	import Icon from './Icon.svelte';

	interface Props {
		open?: boolean;
		title?: string;
		size?: 'md' | 'lg' | 'xl';
		dismissible?: boolean;
		onclose?: () => void;
		footer?: Snippet;
		children: Snippet;
	}

	let {
		open = $bindable(false),
		title,
		size = 'md',
		dismissible = true,
		onclose,
		footer,
		children,
	}: Props = $props();

	const FOCUSABLE =
		'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

	const titleId = `modal-title-${Math.random().toString(36).slice(2, 9)}`;

	let panel = $state<HTMLDivElement | null>(null);

	function requestClose() {
		if (!dismissible) return;
		open = false;
	}

	function handleKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') {
			event.preventDefault();
			requestClose();
			return;
		}
		if (event.key === 'Tab') trapFocus(event);
	}

	function trapFocus(event: KeyboardEvent) {
		if (!panel) return;
		const focusables = Array.from(
			panel.querySelectorAll<HTMLElement>(FOCUSABLE),
		).filter((el) => el.getClientRects().length > 0);
		if (focusables.length === 0) {
			event.preventDefault();
			panel.focus();
			return;
		}
		const first = focusables[0];
		const last = focusables[focusables.length - 1];
		const active = document.activeElement;
		const focusInside = panel.contains(active);
		if (event.shiftKey && (!focusInside || active === first)) {
			event.preventDefault();
			last.focus();
		} else if (!event.shiftKey && (!focusInside || active === last)) {
			event.preventDefault();
			first.focus();
		}
	}

	function handleBackdropClick(event: MouseEvent) {
		if (event.target === event.currentTarget) requestClose();
	}

	$effect(() => {
		if (!open) return;

		const invoker = document.activeElement as HTMLElement | null;
		const previousOverflow = document.body.style.overflow;
		document.body.style.overflow = 'hidden';
		document.addEventListener('keydown', handleKeydown, true);

		requestAnimationFrame(() => {
			if (!panel || panel.contains(document.activeElement)) return;
			const first = panel.querySelector<HTMLElement>(FOCUSABLE);
			(first ?? panel).focus();
		});

		return () => {
			document.body.style.overflow = previousOverflow;
			document.removeEventListener('keydown', handleKeydown, true);
			if (invoker?.isConnected) invoker.focus();
			onclose?.();
		};
	});
</script>

{#if open}
	<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
	<div class="modal-backdrop" onclick={handleBackdropClick}>
		<div
			bind:this={panel}
			class="modal-panel size-{size}"
			role="dialog"
			aria-modal="true"
			aria-labelledby={title ? titleId : undefined}
			aria-label={title ? undefined : 'Dialog'}
			tabindex="-1"
		>
			{#if title || dismissible}
				<div class="modal-header">
					{#if title}
						<h2 class="modal-title" id={titleId}>{title}</h2>
					{/if}
					{#if dismissible}
						<button
							type="button"
							class="modal-close"
							aria-label="Close dialog"
							onclick={requestClose}
						>
							<Icon name="close" size={16} />
						</button>
					{/if}
				</div>
			{/if}
			<div class="modal-body">
				{@render children()}
			</div>
			{#if footer}
				<div class="modal-footer">
					{@render footer()}
				</div>
			{/if}
		</div>
	</div>
{/if}

<style>
	.modal-backdrop {
		position: fixed;
		inset: 0;
		z-index: 900;
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 24px;
		background: rgba(5, 5, 8, 0.6);
		backdrop-filter: blur(6px);
		-webkit-backdrop-filter: blur(6px);
		animation: modal-fade 150ms ease-out;
	}
	.modal-panel {
		width: min(520px, 100%);
		max-height: calc(100vh - 96px);
		display: flex;
		flex-direction: column;
		background: var(--bg-surface);
		border: 1px solid var(--border);
		border-radius: var(--radius-lg);
		box-shadow: 0 24px 64px rgba(0, 0, 0, 0.5);
		outline: none;
		animation: modal-rise 180ms ease-out;
	}
	.modal-panel.size-lg {
		width: min(920px, 100%);
	}
	.modal-panel.size-xl {
		width: min(1180px, 100%);
	}
	.modal-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		padding: 18px 20px 0;
	}
	.modal-title {
		margin: 0;
		font-family: var(--font-display);
		font-size: 16px;
		font-weight: 600;
		color: var(--text-primary);
	}
	.modal-close {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		margin-left: auto;
		border: none;
		border-radius: var(--radius-sm);
		background: transparent;
		color: var(--text-muted);
		cursor: pointer;
		transition:
			background-color 150ms ease,
			color 150ms ease;
	}
	.modal-close:hover {
		background: rgba(255, 255, 255, 0.06);
		color: var(--text-primary);
	}
	.modal-close:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.modal-body {
		padding: 16px 20px 20px;
		overflow-y: auto;
		font-size: 14px;
		line-height: 1.5;
		color: var(--text-primary);
	}
	.modal-footer {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
		padding: 0 20px 20px;
	}
	@keyframes modal-fade {
		from {
			opacity: 0;
		}
		to {
			opacity: 1;
		}
	}
	@keyframes modal-rise {
		from {
			opacity: 0;
			transform: translateY(8px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}
</style>
