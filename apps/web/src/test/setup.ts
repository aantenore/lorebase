import '@testing-library/jest-dom/vitest';
import { cleanup, configure } from '@testing-library/react';
import { afterEach } from 'vitest';

// Tests that opt into the node environment (`@vitest-environment node`) have no DOM to patch.
if (typeof window !== 'undefined') {
  // Screens load their code on demand, and the first load of a route is a cold dynamic import.
  configure({ asyncUtilTimeout: 5000 });

  // jsdom does not implement scrolling; the router calls it on navigation.
  window.scrollTo = () => {};

  // jsdom has no layout; CodeMirror measures text through Range rectangles.
  Range.prototype.getBoundingClientRect = () => new DOMRect();
  Range.prototype.getClientRects = () => ({
    length: 0,
    item: () => null,
    [Symbol.iterator]: [][Symbol.iterator],
  });
}

afterEach(() => {
  cleanup();
  if (typeof window !== 'undefined') {
    window.localStorage.clear();
    document.documentElement.removeAttribute('data-theme');
  }
});
