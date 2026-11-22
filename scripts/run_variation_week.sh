#!/usr/bin/env bash
# Full weekly run for the Top 100 eligible mapped Shopee variations. Stops at the publish preview
# (publishing is the separate, explicit --write-sheets step in docs/MAPPED_TOP_100_2026-11-03.md).
#
#   setsid nohup bash scripts/run_variation_week.sh > /tmp/run_variation_week.out 2>&1 < /dev/null &
#
# Optional environment:
#   SEARCH_CACHE  earlier discovery folder whose saved search/inspection responses are replayed
#                 instead of sent again (default: the newest other run's discovery folder that has
#                 its index files); set SEARCH_CACHE=none to send every request.
#   CACHE_HOURS   replay only responses retrieved within this many hours (default 24).
#   PREV_RUN      earlier run folder: its catalog is diffed in the selection audit.
cd "$(dirname "$0")/.." || exit 1
# A local checkout uses its .venv; CI (GitHub Actions) has no .venv and uses the runner's Python.
if [ -f .venv/bin/activate ]; then source .venv/bin/activate; fi

weekly_run="data/output/weekly_runs/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$weekly_run/inputs"
echo "$weekly_run" > /tmp/current_weekly_run.txt
V="$weekly_run/shopee_variations"
STORE=data/history/price_history.sqlite3
PREV_RUN="${PREV_RUN:-data/output/weekly_runs/20261103T062304Z}"

search_cache="${SEARCH_CACHE:-}"
if [ -z "$search_cache" ]; then
  for d in $(ls -dt data/output/weekly_runs/*/discovery 2>/dev/null); do
    if [ "$d" != "$weekly_run/discovery" ] && [ -f "$d/search_requests.csv" ] \
       && [ -f "$d/product_inspections.csv" ]; then search_cache="$d"; break; fi
  done
fi
cache_args=()
if [ -n "$search_cache" ] && [ "$search_cache" != "none" ]; then
  cache_args=(--search-cache "$search_cache" --cache-max-age-hours "${CACHE_HOURS:-24}")
fi
echo "search cache: ${search_cache:-none}" >> "$weekly_run/driver.log"

stage() {  # stage <name> <accepted exit codes, comma separated> <command...>
  local name="$1" ok="$2"; shift 2
  local start end code
  start=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  echo "=== $name started $start" >> "$weekly_run/driver.log"
  "$@"
  code=$?
  end=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  echo "{\"stage\": \"$name\", \"started_at\": \"$start\", \"finished_at\": \"$end\", \"exit_code\": $code}" >> "$weekly_run/stages.jsonl"
  case ",$ok," in
    *",$code,"*) return 0 ;;
    *) echo "{\"status\": \"FAILED_AT_$name\", \"exit_code\": $code, \"finished_at\": \"$end\"}" > "$weekly_run/status.json"
       echo "=== $name FAILED ($code)" >> "$weekly_run/driver.log"
       exit "$code" ;;
  esac
}

s_sales_once() { python scripts/run_phase1.py --dry-run > "$weekly_run/sales_refresh.log" 2>&1; }
s_sales() {
  if ! s_sales_once; then
    # Standing owner rule (2026-11-03): keep the latest sheet row for conflicting duplicates.
    # Covers only same-date conflicts; anything else stays quarantined and the stage fails.
    cp "$weekly_run/sales_refresh.log" "$weekly_run/sales_refresh_attempt1.log"
    python scripts/apply_latest_row_decisions.py --write > "$weekly_run/latest_row_decisions.log" 2>&1 \
      && s_sales_once || return 1
  fi
  cp data/output/eligible_model_ranking.csv data/output/top_100_targets.csv \
     data/output/phase1_run_summary.json data/output/period_summary.csv \
     data/output/source_shop_names.csv data/output/excluded_listings.csv \
     data/output/eligibility_review.csv "$weekly_run/inputs/"
}
# Reads the three catalog tabs read-only and writes catalog_retrieval.json (time, spreadsheet ID,
# fingerprints) beside the validated copies; the build and the selection audit read it.
s_catalog_check() { python scripts/check_target_tables.py --from-sheets --output-dir "$weekly_run/inputs" > "$weekly_run/catalog_check.log" 2>&1; }
s_build() { python scripts/build_top_shopee_variations.py \
  --ranking "$weekly_run/inputs/eligible_model_ranking.csv" \
  --source-top-100 "$weekly_run/inputs/top_100_targets.csv" \
  --target-master "$weekly_run/inputs/target_master_validated.csv" \
  --source-model-map "$weekly_run/inputs/source_model_map_validated.csv" \
  --match-review "$weekly_run/inputs/match_review_validated.csv" \
  --output-dir "$V" > "$weekly_run/variation_build.log" 2>&1; }
s_audit() { python scripts/audit_top_shopee_variations.py --variation-dir "$V" \
  --ranking "$weekly_run/inputs/eligible_model_ranking.csv" \
  --target-master "$weekly_run/inputs/target_master_validated.csv" \
  --source-model-map "$weekly_run/inputs/source_model_map_validated.csv" \
  --match-review "$weekly_run/inputs/match_review_validated.csv" > "$weekly_run/variation_audit.log" 2>&1; }
s_selection_audit() {
  local extra=()
  [ -f "$PREV_RUN/inputs/target_master_validated.csv" ] && extra+=(--previous-catalog-dir "$PREV_RUN/inputs")
  [ -f "$PREV_RUN/inputs/eligible_model_ranking.csv" ] && extra+=(--compare-ranking "previous_run_pool=$PREV_RUN/inputs/eligible_model_ranking.csv")
  python scripts/audit_variation_selection.py --variation-dir "$V" \
    --ranking "$weekly_run/inputs/eligible_model_ranking.csv" \
    --target-master "$weekly_run/inputs/target_master_validated.csv" \
    --source-model-map "$weekly_run/inputs/source_model_map_validated.csv" \
    --match-review "$weekly_run/inputs/match_review_validated.csv" \
    "${extra[@]}" > "$weekly_run/selection_audit.log" 2>&1
}
# Owner review queue for suspicious catalog bindings (read-only; nothing is applied).
s_corrections() { python scripts/build_catalog_correction_queue.py --run-dir "$weekly_run"   --output-dir "$weekly_run/catalog_corrections" > "$weekly_run/catalog_corrections.log" 2>&1; }
# P1 audit diagnostics prepared as local files and a plan (preview only; no Sheets write).
s_diagnostics() { python scripts/build_publication_diagnostics.py --run-dir "$weekly_run"   --discovery-dir "$weekly_run/discovery"   --correction-queue "$weekly_run/catalog_corrections/catalog_correction_queue.csv"   --output-dir "$weekly_run/diagnostics" > "$weekly_run/diagnostics.log" 2>&1; }
s_catalog_preview() { python scripts/discover_catalog_leads.py --preview --scope-file "$V/context_scope.csv" \
  --target-master "$weekly_run/inputs/target_master_validated.csv" \
  --output-dir "$weekly_run/catalog_leads_preview" > "$weekly_run/catalog_preview.log" 2>&1; }
s_catalog() { python scripts/discover_catalog_leads.py --live --max-total-requests 35 --max-sitemaps-per-retailer 5 \
  --scope-file "$V/context_scope.csv" \
  --target-master "$weekly_run/inputs/target_master_validated.csv" \
  --output-dir "$weekly_run/catalog_leads" > "$weekly_run/catalog.log" 2>&1; }

disc_args=(
  --scope-file "$V/context_scope.csv"
  --target-master "$weekly_run/inputs/target_master_validated.csv"
  --selected-list "$V/context_selected_list.csv"
  --contributions "$V/context_contributions.csv"
  --retailers laptop_factory,datablitz,pcx,pcc,oct
  --max-total-requests 6000 --max-search-requests 4000 --max-search-pages 2
  --max-candidates-per-target-retailer 2 --max-product-fetches 3000
  --max-queries-per-target-retailer 8 --max-seed-urls-per-target-retailer 2
  --max-second-pass-candidates-per-target-retailer 2
  --seed-store "$STORE"
  --supplied-urls config/supplied_candidate_urls.csv
  --supplied-urls "$weekly_run/catalog_leads/catalog_leads.csv"
)
s_disc_preview() { python scripts/discover_products.py --preview "${disc_args[@]}" "${cache_args[@]}" \
  --output-dir "$weekly_run/discovery_preview" > "$weekly_run/discovery_preview.log" 2>&1; }
s_discovery() { python scripts/discover_products.py --live --collect "${disc_args[@]}" "${cache_args[@]}" \
  --store "$STORE" --output-dir "$weekly_run/discovery" > "$weekly_run/discovery.log" 2>&1; }

week_start="$(python -c 'from datetime import datetime, timedelta; from zoneinfo import ZoneInfo; t=datetime.now(ZoneInfo("Asia/Singapore")).date(); print(t - timedelta(days=t.weekday()))')"
as_of_file="$weekly_run/as_of.txt"
# A discovery run that found nothing to collect (every retailer unreachable, no candidate page, no
# historical seed) has no collection folder. That is an outage, not a pipeline failure: the report
# is then built without retailer observations. discovery_summary.json says whether collection ran
# (the readiness gate refuses a run that claims it ran and has no folder).
s_prices() {
  local context_args=()
  [ -f "$weekly_run/discovery/collection/match_results.csv" ] \
    && context_args=(--search-context "$weekly_run/discovery/collection/match_results.csv")
  as_of="$(python -c 'from datetime import UTC, datetime; print(datetime.now(UTC).isoformat())')"
  echo "$as_of" > "$as_of_file"
  python scripts/track_prices.py --store "$STORE" \
    --target-master "$weekly_run/inputs/target_master_validated.csv" \
    --source-model-map "$weekly_run/inputs/source_model_map_validated.csv" \
    --match-review "$weekly_run/inputs/match_review_validated.csv" \
    --selected-list "$V/context_selected_list.csv" --contributions "$V/context_contributions.csv" \
    --sales-ranking "$weekly_run/inputs/eligible_model_ranking.csv" \
    --week-start "$week_start" --as-of "$as_of" --discovery-dir "$weekly_run/discovery" \
    "${context_args[@]}" \
    --output-dir "$weekly_run/context_reports" > "$weekly_run/prices.log" 2>&1
}
s_alt_audit() { python scripts/audit_alternatives.py --report-dir "$weekly_run/context_reports" --store "$STORE" \
  --target-master "$weekly_run/inputs/target_master_validated.csv" \
  --output-dir "$weekly_run/alternative_audit" > "$weekly_run/alt_audit.log" 2>&1; }
s_var_reports() { python scripts/build_variation_reports.py --variation-dir "$V" \
  --context-report-dir "$weekly_run/context_reports" --output-dir "$weekly_run/variation_reports" \
  > "$weekly_run/variation_reports.log" 2>&1; }
s_publish_preview() {
  local collection_args=()
  [ -d "$weekly_run/discovery/collection" ] \
    && collection_args=(--collection-dir "$weekly_run/discovery/collection")
  python scripts/publish_variation_prices.py --preview --variation-dir "$V" \
    --reports-dir "$weekly_run/variation_reports" --context-report-dir "$weekly_run/context_reports" \
    "${collection_args[@]}" --discovery-dir "$weekly_run/discovery" \
    --output-root "$weekly_run/publications" > "$weekly_run/publish_preview.log" 2>&1
}

stage sales_refresh 0 s_sales
stage catalog_check 0 s_catalog_check
stage variation_build 0 s_build
stage variation_audit 0 s_audit
stage selection_audit 0 s_selection_audit
stage catalog_corrections 0 s_corrections
stage catalog_preview 0 s_catalog_preview
stage catalog 0 s_catalog
stage discovery_preview 0 s_disc_preview
stage discovery 0,2 s_discovery
stage prices 0 s_prices
stage alt_audit 0 s_alt_audit
stage variation_reports 0 s_var_reports
stage diagnostics 0 s_diagnostics
stage publish_preview 0 s_publish_preview
echo "{\"status\": \"READY_TO_PUBLISH\", \"finished_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" > "$weekly_run/status.json"
