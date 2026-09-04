window.autoAnimateReady = import("/vendor/auto-animate.mjs")
  .then((module) => {
    window.autoAnimate = module.autoAnimate || module.default;
    return window.autoAnimate;
  })
  .catch((error) => {
    console.warn("Optional list animation is unavailable.", error);
    return null;
  });
