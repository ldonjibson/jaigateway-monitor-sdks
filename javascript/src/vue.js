// Vue 3 (and Vue 2 + Vue Router 3). app.use(InsightVue, { router }): names pages by route pattern and reports errors thrown in components.
import { captureException, setRoute } from 'insight-sdk';
import { fromColonPath } from './route-template.js';

const patternOf = (to) => {
  const m = to && to.matched && to.matched.length ? to.matched[to.matched.length - 1] : null;
  return fromColonPath((m && m.path) || (to && to.path) || '/');
};

export const InsightVue = {
  install(app, options = {}) {
    const previous = app.config.errorHandler;
    app.config.errorHandler = (err, instance, info) => {
      try {
        const name = instance && ((instance.$options && instance.$options.name) || (instance.$ && instance.$.type && (instance.$.type.name || instance.$.type.__name)));
        captureException(err, { tags: { mechanism: 'vue' }, extra: { vue_info: info ? String(info).slice(0, 100) : undefined, component: name ? String(name).slice(0, 100) : undefined } });
      } catch { /* reporting must never break the app */ }
      if (previous) return previous(err, instance, info);
      console.error(err);
    };
    const router = options.router;
    try {
      if (router && typeof router.afterEach === 'function') {
        router.afterEach((to) => { try { setRoute(patternOf(to)); } catch { /* ignore */ } });
        const ready = typeof router.isReady === 'function' ? router.isReady() : Promise.resolve();
        Promise.resolve(ready).then(() => { try { const cur = router.currentRoute && 'value' in router.currentRoute ? router.currentRoute.value : router.currentRoute; if (cur) setRoute(patternOf(cur)); } catch { /* ignore */ } }).catch(() => {});
      }
    } catch { /* a misbehaving router must never break the app: pages just keep their guessed names */ }
  },
};
export default InsightVue;
