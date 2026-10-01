/** Tests for accessible, template-backed tooltip lifecycle behavior. */

import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
    disposeRichTooltips,
    initRichTooltips,
} from './richTooltips';

// The mocks are typed as zero-argument functions on purpose. A bare `vi.fn()`
// is typed in Vitest 5 as "either a function or a constructor", which no
// longer satisfies the `() => void` methods that `initRichTooltips` expects
// of a Bootstrap tooltip instance.
type MockInstance = {
    dispose: ReturnType<typeof vi.fn<() => void>>;
    hide: ReturnType<typeof vi.fn<() => void>>;
};

function buildTooltipApi() {
    /** Build a Bootstrap-compatible constructor with observable instances. */

    const instances = new WeakMap<Element, MockInstance>();
    class MockTooltip {
        dispose = vi.fn<() => void>();
        hide = vi.fn<() => void>();

        constructor(element: HTMLElement) {
            instances.set(element, this);
        }

        static getInstance(element: Element): MockInstance | null {
            return instances.get(element) ?? null;
        }
    }
    return { MockTooltip, instances };
}

describe('rich tooltip lifecycle', () => {
    beforeEach(() => {
        document.body.innerHTML = '';
    });

    it('loads rich HTML from the associated template', () => {
        /** Detailed policy help must not be squeezed into a title attribute. */

        document.body.innerHTML = `
            <span>
              <button data-bs-toggle="tooltip"></button>
              <template class="rich-tooltip-content"><strong>Versioned</strong></template>
            </span>`;
        const { MockTooltip } = buildTooltipApi();
        const trigger = document.querySelector<HTMLElement>('button')!;

        initRichTooltips(document, MockTooltip);

        expect(trigger.getAttribute('title')).toContain('<strong>Versioned</strong>');
    });

    it('does not create duplicate instances after repeated initialization', () => {
        /** HTMX may initialize the same subtree repeatedly without leaking overlays. */

        document.body.innerHTML = '<button data-bs-toggle="tooltip"></button>';
        const { MockTooltip, instances } = buildTooltipApi();
        const trigger = document.querySelector<HTMLElement>('button')!;

        initRichTooltips(document, MockTooltip);
        const first = instances.get(trigger);
        initRichTooltips(document, MockTooltip);

        expect(instances.get(trigger)).toBe(first);
    });

    it('initializes tooltip content inside an HTMX replacement root', () => {
        /** Newly swapped fields need the same keyboard-accessible help as first load. */

        const root = document.createElement('section');
        root.innerHTML = '<button data-bs-toggle="tooltip"></button>';
        const { MockTooltip, instances } = buildTooltipApi();
        const trigger = root.querySelector<HTMLElement>('button')!;

        initRichTooltips(root, MockTooltip);

        expect(instances.get(trigger)).toBeTruthy();
    });

    it('disposes an instance before its HTMX subtree is replaced', () => {
        /** Disposal prevents an open tooltip from becoming an orphaned body node. */

        const root = document.createElement('section');
        root.innerHTML = '<button data-bs-toggle="tooltip"></button>';
        const { MockTooltip, instances } = buildTooltipApi();
        const trigger = root.querySelector<HTMLElement>('button')!;
        initRichTooltips(root, MockTooltip);
        const instance = instances.get(trigger)!;

        disposeRichTooltips(root, MockTooltip);

        expect(instance.dispose).toHaveBeenCalledOnce();
    });
});
