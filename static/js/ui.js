// The behaviour templates ask for by attribute, so that no page carries an inline handler
// and script-src can stay closed to inline code. A handler written into the markup needs
// 'unsafe-inline', which is the whole of what the policy is there to refuse.
//
// Delegated from the document, so this reaches markup htmx swapped in as readily as markup
// that arrived with the page.
(() => {
    const byId = id => document.getElementById(id);

    document.addEventListener('click', event => {
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
            htmx.ajax('GET', opener.dataset.loads, opener.dataset.into);
        }
        byId(opener.dataset.opens).showModal();
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
