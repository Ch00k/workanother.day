// The behaviour templates ask for by attribute, so that no page carries an inline handler
// and script-src can stay closed to inline code. A handler written into the markup needs
// 'unsafe-inline', which is the whole of what the policy is there to refuse.
//
// Delegated from the document, so this reaches markup htmx swapped in as readily as markup
// that arrived with the page.
(() => {
    const byId = id => document.getElementById(id);

    const escaped = text => {
        const node = document.createElement('div');
        node.textContent = text;
        return node.innerHTML;
    };

    // Marked, so that the reply arriving is told apart from it never having arrived.
    const pending = text => `<div data-pending class="text-sm text-[#888]">${escaped(text)}</div>`;
    const failed = text => `<div class="notice notice-error">${escaped(text)}</div>`;

    const closeExplainers = () => {
        document.querySelectorAll('[data-explains][aria-expanded="true"]').forEach(button => {
            button.setAttribute('aria-expanded', 'false');
            byId(button.dataset.explains).hidden = true;
        });
    };

    // Closed by a press that starts outside it, rather than by any click anywhere: a click
    // whose press began in the panel is somebody selecting the text to copy it, and a panel
    // that vanished on release would take the selection with it. Escape closes it too, below.
    document.addEventListener('pointerdown', event => {
        if (event.target.closest('.explainer-panel, [data-explains]')) return;

        closeExplainers();
    });

    document.addEventListener('click', event => {
        const explaining = event.target.closest('[data-explains]');
        if (explaining) {
            const panel = byId(explaining.dataset.explains);
            const opening = panel.hidden;

            // One open at a time, so a page of cards does not end up a page of panels.
            closeExplainers();
            panel.hidden = !opening;
            explaining.setAttribute('aria-expanded', String(opening));
            return;
        }

        // A click landing on the dialog itself is a click on its backdrop: anything inside
        // it has a child as the target.
        if (event.target.matches('[data-closes-on-backdrop]')) {
            event.target.close();
            return;
        }

        const closer = event.target.closest('[data-closes-dialog]');
        if (closer) {
            closer.closest('dialog').close();
            return;
        }

        const copier = event.target.closest('[data-copies]');
        if (copier) {
            navigator.clipboard.writeText(copier.dataset.copies);
            return;
        }

        if (event.target.closest('[data-prints]')) {
            window.print();
            return;
        }

        // Stands in for a control that is styled out of the way, the file input behind an
        // Import button being the one that needs it.
        const forwarder = event.target.closest('[data-clicks]');
        if (forwarder) {
            byId(forwarder.dataset.clicks).click();
            return;
        }

        const opener = event.target.closest('[data-opens]');
        if (!opener) return;

        // Filled before it is shown, for the dialogs whose contents are fetched.
        if (opener.dataset.loads) {
            const target = document.querySelector(opener.dataset.into);

            // The dialog opens straight away rather than waiting on the answer, so what it
            // opens on has to say that one is coming. Written over whatever the last open
            // left there, which is a comparison that has since been overtaken.
            target.innerHTML = pending(opener.dataset.pending || 'Loading...');

            // htmx swaps nothing when the request fails, so the line above is still there,
            // and still promising an answer that is not coming. Settled either way.
            htmx.ajax('GET', opener.dataset.loads, opener.dataset.into).then(() => {
                if (target.querySelector('[data-pending]')) {
                    target.innerHTML = failed(opener.dataset.failed || 'Could not load this.');
                }
            });
        }
        byId(opener.dataset.opens).showModal();
    });

    // Escape puts the reader back on the mark they opened, which is where they were.
    document.addEventListener('keydown', event => {
        if (event.key !== 'Escape') return;

        const open = document.querySelector('[data-explains][aria-expanded="true"]');
        if (!open) return;

        closeExplainers();
        open.focus();
    });

    document.addEventListener('submit', event => {
        const question = event.target.getAttribute('data-confirm');
        if (question !== null && !window.confirm(question)) {
            event.preventDefault();
        }
    });

    // Choosing a file is the whole of that form, so there is nothing further to fill in.
    // requestSubmit rather than submit, so the form's own validation and any question above
    // still apply.
    document.addEventListener('change', event => {
        if (event.target.closest('[data-submits]')) {
            event.target.form.requestSubmit();
        }
    });
})();
