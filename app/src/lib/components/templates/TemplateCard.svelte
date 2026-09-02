<script lang="ts">
	import { Star, Pencil, Trash2, Eye, FileText } from '@lucide/svelte';
	import type { TemplateOut } from '$lib/api/client';

	interface Props {
		template: TemplateOut;
		onEdit: (t: TemplateOut) => void;
		onDelete: (id: string) => void;
		onPreview: (t: TemplateOut) => void;
	}

	let { template, onEdit, onDelete, onPreview }: Props = $props();

	const formatColors: Record<string, string> = {
		pdf: 'bg-rose-950/40 text-rose-300 border-rose-500/20',
		html: 'bg-cyan-950/40 text-cyan-300 border-cyan-500/20',
		docx: 'bg-blue-950/40 text-blue-300 border-blue-500/20'
	};

	const colCount = $derived(
		(template.schema_definition?.columns as string[] | undefined)?.length ?? 0
	);
	const themeColor = $derived(template.schema_definition?.theme_color as string | undefined);
	const updatedLabel = $derived(
		new Date(template.updated_at).toLocaleDateString('en-US', {
			month: 'short',
			day: 'numeric',
			year: 'numeric'
		})
	);
</script>

<div
	class="group relative bg-surface-900 border border-white/8 hover:border-orbit-cyan/40 rounded-2xl p-4 flex flex-col gap-3 transition-all shadow-lg hover:shadow-orbit-cyan/10"
>
	{#if template.is_default}
		<div class="absolute top-3 right-3">
			<Star size={13} class="text-amber-400 fill-amber-400" />
		</div>
	{/if}

	<div class="flex items-start gap-3">
		<div class="p-2 rounded-lg bg-surface-800 border border-white/5 text-orbit-cyan shrink-0">
			<FileText size={18} />
		</div>
		<div class="min-w-0 flex-1">
			<div class="flex items-center gap-2 flex-wrap">
				<span class="text-sm font-semibold text-slate-100 truncate font-display"
					>{template.name}</span
				>
				<span
					class="text-[9px] font-mono uppercase px-1.5 py-0.5 rounded border {formatColors[
						template.format
					] ?? formatColors.pdf}"
				>
					{template.format.toUpperCase()}
				</span>
			</div>
			{#if template.description}
				<p class="text-[11px] font-mono text-slate-400 mt-0.5 line-clamp-2">
					{template.description}
				</p>
			{/if}
		</div>
	</div>

	<div class="flex items-center gap-2 flex-wrap">
		{#if colCount > 0}
			<span
				class="text-[10px] font-mono px-2 py-0.5 rounded bg-surface-800 border border-white/5 text-slate-400"
				>{colCount} column{colCount !== 1 ? 's' : ''}</span
			>
		{/if}
		{#if themeColor}
			<span
				class="flex items-center gap-1 text-[10px] font-mono px-2 py-0.5 rounded bg-surface-800 border border-white/5 text-slate-400"
			>
				<span class="w-2 h-2 rounded-full" style="background:{themeColor}"></span>Themed
			</span>
		{/if}
		{#if template.schema_definition?.show_summary !== false}
			<span
				class="text-[10px] font-mono px-2 py-0.5 rounded bg-surface-800 border border-white/5 text-slate-400"
				>Summary block</span
			>
		{/if}
	</div>

	<p class="text-[10px] font-mono text-slate-600">Updated {updatedLabel}</p>

	<div
		class="pt-2 border-t border-white/8 flex items-center gap-1.5 opacity-0 group-hover:opacity-100 transition-opacity"
	>
		<button
			type="button"
			onclick={() => onPreview(template)}
			class="flex-1 flex items-center justify-center gap-1.5 py-1.5 text-[11px] font-mono rounded-lg bg-surface-800 hover:bg-surface-700 border border-white/5 text-slate-300 hover:text-orbit-cyan transition-colors"
		>
			<Eye size={12} />Preview
		</button>
		<button
			type="button"
			onclick={() => onEdit(template)}
			class="flex-1 flex items-center justify-center gap-1.5 py-1.5 text-[11px] font-mono rounded-lg bg-surface-800 hover:bg-surface-700 border border-white/5 text-slate-300 hover:text-white transition-colors"
		>
			<Pencil size={12} />Edit
		</button>
		<button
			type="button"
			onclick={() => onDelete(template.id)}
			class="p-1.5 rounded-lg bg-surface-800 hover:bg-rose-950/50 border border-white/5 text-slate-400 hover:text-rose-400 hover:border-rose-500/30 transition-colors"
			title="Delete template"
		>
			<Trash2 size={12} />
		</button>
	</div>
</div>
