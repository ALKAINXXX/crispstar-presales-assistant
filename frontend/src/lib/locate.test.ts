import { expect, it, vi } from 'vitest';
import { locateThreadElement } from './locate';

it('reply location scrolls only the real Thread viewport, with bounded offset and no focus-induced page scrolling', () => {
  document.body.innerHTML = '<header>页头</header><div class="aui-thread-viewport" data-slot="aui_thread-viewport"><article id="message-real" tabindex="-1">原回复</article></div><div id="outside">另一个区域</div>';
  const viewport = document.querySelector<HTMLElement>('.aui-thread-viewport')!;
  const target = document.getElementById('message-real')!;
  Object.defineProperties(viewport, {scrollHeight:{value:1200},clientHeight:{value:400},scrollTop:{value:250,writable:true}});
  viewport.getBoundingClientRect = () => ({top:100} as DOMRect);
  target.getBoundingClientRect = () => ({top:700} as DOMRect);
  const scroll = vi.spyOn(viewport,'scrollTo');
  const focus = vi.spyOn(target,'focus');
  const pageScroll = vi.spyOn(window,'scrollTo');
  const scrollIntoView = vi.spyOn(target,'scrollIntoView');
  expect(locateThreadElement('message-real')).toBe(true);
  expect(scroll).toHaveBeenLastCalledWith({top:800,behavior:'smooth'});
  expect(focus).toHaveBeenCalledWith({preventScroll:true});
  expect(pageScroll).not.toHaveBeenCalled();
  expect(scrollIntoView).not.toHaveBeenCalled();
  target.getBoundingClientRect = () => ({top:-300} as DOMRect);
  expect(locateThreadElement('message-real')).toBe(true);
  expect(scroll).toHaveBeenLastCalledWith({top:0,behavior:'smooth'});
  scroll.mockRestore(); focus.mockRestore(); pageScroll.mockRestore(); scrollIntoView.mockRestore();
  document.body.innerHTML = '';
});

it('a drawer or missing message cannot be treated as a thread reply location', () => {
  document.body.innerHTML = '<div class="aui-thread-viewport" data-slot="aui_thread-viewport"></div><div id="drawer-result" tabindex="-1">当前状态</div>';
  const viewport = document.querySelector<HTMLElement>('.aui-thread-viewport')!;
  const scroll = vi.spyOn(viewport,'scrollTo');
  expect(locateThreadElement('drawer-result')).toBe(false);
  expect(locateThreadElement('message-missing')).toBe(false);
  expect(scroll).not.toHaveBeenCalled();
  scroll.mockRestore(); document.body.innerHTML = '';
});
