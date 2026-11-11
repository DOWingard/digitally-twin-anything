/* Callbacks that run in the browser: they wait for the operator to pause, and ask grids to
   read their rows again. Neither reads the twin: the server does that, through the API. */

(function () {
  // How long a slider or a search field must stay unchanged before anything is read;
  // the same value as SETTLE_MS in header.py.
  var SETTLE_MS = 300;
  var timers = {};

  // Hands `value` on to the `prop` of `target` once it has stayed the same for SETTLE_MS.
  // `current` is what `target` holds now: a value that is already there is not sent again.
  function settle(key, target, prop, value, current) {
    var dc = window.dash_clientside;
    clearTimeout(timers[key]);
    delete timers[key];
    if (value === undefined || value === null || value === "" || value === current) {
      return dc.no_update;
    }
    timers[key] = setTimeout(function () {
      delete timers[key];
      var props = {};
      props[prop] = value;
      dc.set_props(target, props);
    }, SETTLE_MS);
    return dc.no_update;
  }

  // Asks each grid of the infinite row model to read the blocks it holds again; the grid
  // stays, with its scroll position, sort and filters. A grid that is not drawn (its tab
  // is not open) is skipped: it reads afresh when it is drawn.
  function refresh(token, gridIds) {
    var dc = window.dash_clientside;
    if (token === undefined || token === null) {
      return dc.no_update;
    }
    (gridIds || []).forEach(function (gridId) {
      var api = null;
      try {
        api = window.dash_ag_grid.getApi(gridId);
      } catch (error) {
        return;
      }
      if (api && !(api.isDestroyed && api.isDestroyed())) {
        api.refreshInfiniteCache();
      }
    });
    return dc.no_update;
  }

  // Added to the namespace object Dash's renderer also writes into, never replacing it.
  window.dash_clientside = window.dash_clientside || {};
  window.dash_clientside.dta = {
    settleVersion: function (value, settled) {
      return settle("version", "settled-version", "data", value, settled);
    },
    settleSearch: function (text, settled) {
      return settle("search", "graph-search-text", "data", text, settled);
    },
    refreshGrids: refresh,
  };
})();
