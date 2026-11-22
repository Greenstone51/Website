document.addEventListener("DOMContentLoaded", () => {
    const menuButton = document.getElementById("navToggle");
    const mobileMenu = document.getElementById("navMobile");
    if (menuButton && mobileMenu) {
        menuButton.addEventListener("click", () => {
            const isOpen = mobileMenu.classList.toggle("open");
            menuButton.setAttribute("aria-expanded", String(isOpen));
            menuButton.setAttribute("aria-label", isOpen ? "Menü schließen" : "Menü öffnen");
        });
    }

    document.querySelectorAll("[data-copy-address]").forEach((button) => {
        button.addEventListener("click", async () => {
            const address = button.dataset.copyAddress;
            const originalLabel = button.dataset.originalLabel || button.textContent;
            button.dataset.originalLabel = originalLabel;
            try {
                if (navigator.clipboard && window.isSecureContext) {
                    await navigator.clipboard.writeText(address);
                } else {
                    const helper = document.createElement("textarea");
                    helper.value = address;
                    helper.setAttribute("readonly", "");
                    helper.style.position = "fixed";
                    helper.style.opacity = "0";
                    document.body.appendChild(helper);
                    helper.select();
                    const copied = document.execCommand("copy");
                    helper.remove();
                    if (!copied) throw new Error("Clipboard API unavailable");
                }
                button.textContent = "Kopiert";
                window.setTimeout(() => { button.textContent = originalLabel; }, 1800);
            } catch (error) {
                window.prompt("Serveradresse manuell kopieren:", address);
            }
        });
    });

    document.querySelectorAll("[data-youtube-video]").forEach((container) => {
        const loadButton = container.querySelector("[data-load-youtube-video]");
        if (!loadButton) return;
        loadButton.addEventListener("click", () => {
            const videoId = container.dataset.youtubeVideo;
            if (!/^[A-Za-z0-9_-]{11}$/.test(videoId)) return;
            const iframe = document.createElement("iframe");
            iframe.src = "https://www.youtube-nocookie.com/embed/" + encodeURIComponent(videoId) + "?autoplay=1&rel=0";
            iframe.title = container.dataset.videoTitle || "Neuestes YouTube-Video von Greenstone51";
            iframe.loading = "eager";
            iframe.referrerPolicy = "origin";
            iframe.allow = "accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share";
            iframe.allowFullscreen = true;
            container.replaceChildren(iframe);
        });
    });
});
