/** Locate a real reply/result inside the Thread viewport without scrolling the page. */
export function locateThreadElement(id: string, ownerDocument: Document = document): boolean {
  const target = ownerDocument.getElementById(id);
  const viewport = target?.closest<HTMLElement>('[data-slot="aui_thread-viewport"]');
  if (!viewport || !target || !viewport.contains(target)) return false;
  const offset = viewport.scrollTop + target.getBoundingClientRect().top - viewport.getBoundingClientRect().top - 16;
  const top = Math.max(0, Math.min(Math.max(0, viewport.scrollHeight - viewport.clientHeight), offset));
  const reducedMotion = ownerDocument.defaultView?.matchMedia('(prefers-reduced-motion: reduce)').matches;
  viewport.scrollTo({top, behavior: reducedMotion ? 'auto' : 'smooth'});
  target.focus({preventScroll:true});
  return true;
}
