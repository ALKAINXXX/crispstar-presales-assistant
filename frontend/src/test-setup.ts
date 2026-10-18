// Browser geometry is not emulated here. These shims let the real components
// mount for deterministic mapping tests; real viewport QA remains separate.
import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

afterEach(cleanup);
Object.defineProperty(window, 'matchMedia', { writable: true, value: (query: string) => ({ matches: false, media: query, onchange: null, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent() { return false; } }) });
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as typeof ResizeObserver;
globalThis.IntersectionObserver = class { observe() {} unobserve() {} disconnect() {} takeRecords() { return []; } readonly root = null; readonly rootMargin = ''; readonly thresholds = []; } as unknown as typeof IntersectionObserver;
Element.prototype.scrollIntoView = function () {};
Element.prototype.scrollTo = function () {};
