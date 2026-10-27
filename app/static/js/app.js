/**
 * Drone CI Build Sync - Frontend JavaScript
 */

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `pointer-events-auto flex items-center gap-2.5 px-4 py-3 rounded-xl border text-xs shadow-xl transition-all duration-300 transform translate-y-2 opacity-0`;
    
    if (type === 'success') {
        toast.className += ' bg-emerald-950/90 text-emerald-200 border-emerald-800';
        toast.innerHTML = `<span class="text-base">✅</span><span>${message}</span>`;
    } else if (type === 'error') {
        toast.className += ' bg-rose-950/90 text-rose-200 border-rose-800';
        toast.innerHTML = `<span class="text-base">❌</span><span>${message}</span>`;
    } else if (type === 'warning') {
        toast.className += ' bg-amber-950/90 text-amber-200 border-amber-800';
        toast.innerHTML = `<span class="text-base">⚠️</span><span>${message}</span>`;
    } else {
        toast.className += ' bg-slate-900/90 text-slate-200 border-slate-700';
        toast.innerHTML = `<span class="text-base">ℹ️</span><span>${message}</span>`;
    }

    container.appendChild(toast);

    // Animation in
    requestAnimationFrame(() => {
        toast.classList.remove('translate-y-2', 'opacity-0');
    });

    // Auto dismiss after 4s
    setTimeout(() => {
        toast.classList.add('opacity-0', 'translate-y-2');
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}

async function triggerRepoCheck(repoId, btn) {
    if (btn) {
        btn.classList.add('animate-spin', 'text-cyan-400');
        btn.disabled = true;
    }
    showToast("Запущен опрос репозитория...", "info");

    try {
        const resp = await fetch(`/api/repos/${repoId}/check-now`, { method: 'POST' });
        const data = await resp.json();

        if (resp.ok) {
            if (data.status === 'ok') {
                const count = data.triggers_count || 0;
                if (count > 0) {
                    showToast(`Опрос завершён! Запущено сборок: ${count}`, "success");
                } else {
                    showToast(`Опрос завершён. Новых коммитов нет (проверено веток: ${data.branches_checked})`, "info");
                }
            } else if (data.status === 'skipped') {
                showToast("Репозиторий на паузе", "warning");
            } else {
                showToast(`Ошибка: ${data.message}`, "error");
            }
            // Auto reload current view to show refreshed timestamps & statuses
            setTimeout(() => window.location.reload(), 1200);
        } else {
            showToast(data.detail || "Ошибка выполнения опроса", "error");
        }
    } catch (err) {
        showToast("Сетевая ошибка при запуске проверки", "error");
    } finally {
        if (btn) {
            btn.classList.remove('animate-spin', 'text-cyan-400');
            btn.disabled = false;
        }
    }
}

async function toggleRepoActive(repoId, btn) {
    try {
        const resp = await fetch(`/api/repos/${repoId}/toggle-active`, { method: 'POST' });
        const data = await resp.json();
        if (resp.ok) {
            showToast(data.is_active ? "Репозиторий активирован" : "Репозиторий поставлен на паузу", "success");
            setTimeout(() => window.location.reload(), 600);
        } else {
            showToast("Не удалось изменить статус", "error");
        }
    } catch (err) {
        showToast("Сетевая ошибка", "error");
    }
}
