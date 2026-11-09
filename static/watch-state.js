/* Save watchlists in this browser. Saving or viewing sends no API request. */
(function (root) {
  function signature(request = {}) {
    // Same query and settings means one watch, even if market order changes.
    return JSON.stringify({
      query: String(request.query || '').trim().toLowerCase().replace(/\s+/g, ' '),
      source: request.source_mode || 'live',
      markets: [...new Set(request.marketplaces || [])].sort(),
      matching: request.match_mode || 'exact',
      pricing: request.pricing_mode || (request.resale_estimate != null ? 'manual' : 'auto'),
      resale: request.resale_estimate ?? null,
      ceiling: request.max_purchase_price ?? null,
    });
  }

  function watchState(request, response, items = []) {
    if (!request || !response) return {kind:'unavailable', message:'Run a search first, then save it.'};
    if (request.lens_reference_id || response.lens_context) {
      return {kind:'unsupported', message:'Lens photos are temporary. Run a text-only live search to save a watchlist.'};
    }
    if (request.source_mode !== 'live') {
      return {kind:'unsupported', message:'Demo results cannot be watched. Run a Live market search first.'};
    }
    const saved = items.find((item) => item?.request && signature(item.request) === signature(request));
    return saved ? {kind:'saved', id:saved.id, message:'Saved in this browser · Scan manually'} : {kind:'ready', message:''};
  }

  function saveWatchlist(storage, storageKey, request, response, items = [], now = new Date().toISOString()) {
    // Do not claim success until storage.setItem() has actually succeeded.
    const state = watchState(request, response, items);
    if (state.kind === 'saved') return {ok:true, alreadySaved:true, id:state.id};
    if (state.kind !== 'ready') return {ok:false, message:state.message};
    // Save the current price and buy-count baseline so a later manual scan can compare it.
    const offers = response.decisions || [];
    const prices = offers.map((item) => item.price).filter((price) => typeof price === 'number' && Number.isFinite(price));
    const item = {
      id: `watch-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      signature: signature(request), created_at:now, last_checked_at:now,
      request: JSON.parse(JSON.stringify(request)),
      best_price: prices.length ? Math.min(...prices) : null,
      buy_count: offers.filter((offer) => offer.is_profitable).length,
    };
    try {
      storage.setItem(storageKey, JSON.stringify([item, ...items]));
    } catch (_) {
      return {ok:false, message:'Could not save this watchlist. Browser storage is full or blocked. Free some space or allow site storage, then try again.'};
    }
    return {ok:true, alreadySaved:false, id:item.id};
  }

  const state = {signature, watchState, saveWatchlist};
  if (typeof module !== 'undefined' && module.exports) module.exports = state;
  else root.ArbiWatchState = state;
})(typeof globalThis !== 'undefined' ? globalThis : this);
