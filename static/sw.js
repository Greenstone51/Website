self.addEventListener('push', function(event) {
    let data = {};
    if (event.data) {
        try {
            data = event.data.json();
        } catch (e) {
            data = { title: 'Greenstone51 Upload', body: event.data.text() };
        }
    } else {
        data = { title: 'Greenstone51 Upload', body: 'Neue Datei hochgeladen!' };
    }

    const options = {
        body: data.body || 'Eine neue Datei steht zum Download bereit.',
        icon: '/static/favicon.svg',
        badge: '/static/favicon.svg',
        vibrate: [80, 40, 500, 40, 80],
        data: {
            url: data.url || '/download'
        }
    };

    event.waitUntil(
        self.registration.showNotification(data.title || 'Greenstone51 Upload', options)
    );
});

self.addEventListener('notificationclick', function(event) {
    event.notification.close();
    const targetUrl = event.notification.data && event.notification.data.url ? event.notification.data.url : '/download';

    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(clientList) {
            for (let i = 0; i < clientList.length; i++) {
                let client = clientList[i];
                if (client.url.includes(targetUrl) && 'focus' in client) {
                    return client.focus();
                }
            }
            if (clients.openWindow) {
                return clients.openWindow(targetUrl);
            }
        })
    );
});
