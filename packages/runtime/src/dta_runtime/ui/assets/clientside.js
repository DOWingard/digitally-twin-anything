/* Callbacks that run in the browser: they wait for the operator to pause, ask grids to read
   their rows again, and mark the selected version's row. None reads the twin: the server
   does that, through the API. */

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

  // The grid drawn under a Dash id, or null when it is not drawn (its tab is not open).
  function gridApi(gridId) {
    var api = null;
    try {
      api = window.dash_ag_grid.getApi(gridId);
    } catch (error) {
      return null;
    }
    return api && !(api.isDestroyed && api.isDestroyed()) ? api : null;
  }

  // Asks each grid of the infinite row model to read the blocks it holds again; the grid
  // stays, with its scroll position, sort and filters. A grid that is not drawn is
  // skipped: it reads afresh when it is drawn.
  function refresh(token, gridIds) {
    var dc = window.dash_clientside;
    if (token === undefined || token === null) {
      return dc.no_update;
    }
    (gridIds || []).forEach(function (gridId) {
      var api = gridApi(gridId);
      if (api) {
        api.refreshInfiniteCache();
      }
    });
    return dc.no_update;
  }

  // The seq of the version whose row is shown selected, and the grids that mark it again
  // each time their rows change.
  var selectedSeq = null;
  var watched = new WeakSet();

  function markRow(api) {
    if (selectedSeq === null) {
      return;
    }
    var node = api.getRowNode(String(selectedSeq));
    if (node) {
      if (!node.isSelected()) {
        node.setSelected(true, true);
      }
    } else if (api.getSelectedNodes().length) {
      api.deselectAll();
    }
  }

  function markVersionRow(_requests, version, gridIds) {
    selectedSeq = version ? version.seq : null;
    (gridIds || []).forEach(function (gridId) {
      var api = gridApi(gridId);
      if (!api) {
        return;
      }
      if (!watched.has(api)) {
        watched.add(api);
        api.addEventListener("modelUpdated", function () {
          markRow(api);
        });
      }
      markRow(api);
    });
    return window.dash_clientside.no_update;
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
    markVersionRow: markVersionRow,
  };
})();
