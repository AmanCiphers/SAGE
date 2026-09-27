export default function Panel({ title, meta, children, className = "", bodyClassName = "" }) {
  return (
    <section
      className={`flex flex-col border border-zinc-800 bg-zinc-950 ${className}`}
    >
      <header className="flex items-center justify-between border-b border-zinc-800 px-3 py-2">
        <span className="text-[10px] uppercase tracking-[0.25em] text-zinc-500">
          {title}
        </span>
        {meta ? <span className="text-[10px] text-zinc-700">{meta}</span> : null}
      </header>
      <div className={`flex-1 overflow-y-auto ${bodyClassName}`}>{children}</div>
    </section>
  );
}
