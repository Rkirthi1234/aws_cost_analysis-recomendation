"""Equivalent-workload estimates using exact AWS Price List matches and Decimal.

No prices are guessed. Ambiguous SKUs, failed pagination, missing cost components,
unknown commitment effects and missing benchmark inputs block savings claims.
"""
import json
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from agent.services.compatibility_evaluator import fargate_size, number, timestamp
from agent.services.recommendation_evidence import validate_record
from agent.services.readonly_guard import ReadOnlySessionManager

GATEWAY_PRICING_URL = "https://docs.aws.amazon.com/vpc/latest/privatelink/gateway-endpoints.html"


class PricingUnavailable(ValueError):
    pass


def decimal(value):
    if isinstance(value, bool):
        raise PricingUnavailable("Boolean values are not costs or quantities.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise PricingUnavailable("A numeric, non-negative cost or quantity is required.")
    if not result.is_finite() or result < 0:
        raise PricingUnavailable("Costs and quantities must be finite and non-negative.")
    return result


def money(value):
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class AWSPriceList:
    def __init__(self, session_manager, ttl_seconds=86400, now_fn=None):
        self.sm = ReadOnlySessionManager(session_manager)
        self.ttl = ttl_seconds
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.cache = {}

    def quote(self, account_id, region, query, quantity):
        if not isinstance(query, dict) or not isinstance(query.get("filters"), dict):
            raise PricingUnavailable("A rate query with explicit AWS product filters is required.")
        service, unit = query.get("service_code"), query.get("unit")
        if not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9]{1,128}", service) or not isinstance(unit, str) or not unit:
            raise PricingUnavailable("Unsupported pricing service or missing unit.")
        attrs = dict(query["filters"])
        if attrs.get("regionCode", region) != region:
            raise PricingUnavailable("Pricing query region does not match the workload.")
        attrs["regionCode"] = region
        usage_suffix = query.get("usage_type_suffix")
        if usage_suffix is not None and (not isinstance(usage_suffix, str) or not usage_suffix):
            raise PricingUnavailable("usage_type_suffix must be a non-empty exact suffix.")
        if not all(isinstance(k, str) and k and isinstance(v, str) and v for k, v in attrs.items()):
            raise PricingUnavailable("Price filters must be non-empty strings.")
        key = (account_id, region, service, unit, json.dumps(attrs, sort_keys=True), usage_suffix)
        cached = self.cache.get(key)
        if cached and time.monotonic() - cached[0] < self.ttl:
            products, retrieved_at = cached[1:]
        else:
            client = self.sm.get_client(account_id, "pricing", "us-east-1")
            products, token, seen = [], None, set()
            for _ in range(50):
                params = {"ServiceCode": service, "Filters": [
                    {"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in sorted(attrs.items())], "MaxResults": 100}
                if token:
                    params["NextToken"] = token
                response = client.get_products(**params)
                for item in response.get("PriceList", []):
                    product = json.loads(item) if isinstance(item, str) else item
                    actual = {**product.get("product", {}).get("attributes", {}),
                              "productFamily": product.get("product", {}).get("productFamily")}
                    if any(actual.get(k) != v for k, v in attrs.items()):
                        raise PricingUnavailable("AWS returned a product outside the requested price filters.")
                    if usage_suffix and not actual.get("usagetype", "").endswith(usage_suffix):
                        continue
                    products.append(product)
                token = response.get("NextToken")
                if not token:
                    break
                if token in seen:
                    raise PricingUnavailable("Pricing pagination repeated a token.")
                seen.add(token)
            else:
                raise PricingUnavailable("Pricing pagination was truncated.")
            retrieved_at = self.now_fn().isoformat()
            if len(self.cache) >= 512:
                self.cache.clear()
            self.cache[key] = (time.monotonic(), products, retrieved_at)
        choices = {}
        now = self.now_fn()
        for product in products:
            terms = list(product.get("terms", {}).get("OnDemand", {}).values())
            terms = [t for t in terms if timestamp(t.get("effectiveDate")) and timestamp(t["effectiveDate"]) <= now]
            if not terms:
                continue
            latest = max(timestamp(t["effectiveDate"]) for t in terms)
            for term in terms:
                if timestamp(term["effectiveDate"]) != latest:
                    continue
                dims = [d for d in term.get("priceDimensions", {}).values() if d.get("unit") == unit]
                if dims:
                    sku = product.get("product", {}).get("sku")
                    choices[(sku, term.get("offerTermCode"), term.get("effectiveDate"))] = (product, term, dims)
        if len(choices) != 1:
            raise PricingUnavailable(f"Expected one exact {service}/{unit} SKU; found {len(choices)}. Refine price filters.")
        product, term, dims = next(iter(choices.values()))
        tiers = []
        for dim in dims:
            if dim.get("appliesTo"):
                raise PricingUnavailable("Conditional AWS rate dimensions need additional usage attribution.")
            if "USD" not in dim.get("pricePerUnit", {}):
                raise PricingUnavailable("The selected rate has no USD price.")
            low = decimal(dim.get("beginRange"))
            high = Decimal("Infinity") if dim.get("endRange") == "Inf" else decimal(dim.get("endRange"))
            if high <= low:
                raise PricingUnavailable("Invalid AWS pricing tier.")
            tiers.append((low, high, decimal(dim["pricePerUnit"]["USD"]), dim.get("rateCode")))
        tiers.sort(key=lambda t: t[0])
        if not tiers or tiers[0][0] != 0 or tiers[-1][1] != Decimal("Infinity") or any(a[1] != b[0] for a, b in zip(tiers, tiers[1:])):
            raise PricingUnavailable("Price tiers are incomplete, overlapping or discontinuous.")
        if len(tiers) > 1 and "account_prior_units" not in query:
            raise PricingUnavailable("Tiered pricing requires measured account_prior_units; no tier or free allowance is assumed.")
        prior = decimal(query.get("account_prior_units", 0))
        quantity = decimal(quantity)
        end = prior + quantity
        cost = sum((max(Decimal(0), min(end, high) - max(prior, low)) * rate for low, high, rate, _ in tiers), Decimal(0))
        return {"amount_exact": str(cost), "amount_usd": money(cost), "quantity": str(quantity),
                "unit": unit, "sku": product["product"]["sku"], "service_code": service,
                "filters": attrs, "effective_date": term["effectiveDate"], "retrieved_at": retrieved_at,
                "usage_type_suffix": usage_suffix,
                "publication_date": product.get("publicationDate"), "currency": "USD",
                "tiers": [{"begin": str(a), "end": str(b), "rate_usd": str(c), "rate_code": d} for a, b, c, d in tiers],
                "basis": "AWS public OnDemand rates; tax, free tier and private discounts excluded"}


class AlternativeCostComparison:
    def __init__(self, prices, settings, now_fn=None):
        self.prices, self.settings = prices, settings
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    def compare(self, entry, decision, resource, inputs):
        if decision["verdict"] != "compatible":
            return {"status": "not_evaluated", "reason": "A compatible verdict is required before pricing."}
        profile = inputs.get("pricing") if isinstance(inputs, dict) else None
        scope = {k: decision[k] for k in ("account_id", "region", "resource_id")}
        try:
            issues = self.settings.issues_affecting({"pricing.hours_per_month", "pricing.currency", "pricing.min_monthly_savings_usd",
                                                   "pricing.min_savings_pct", "cache.ttl_seconds"})
            if issues:
                raise PricingUnavailable("Invalid pricing configuration: " + "; ".join(i.message for i in issues))
            error = validate_record(profile, scope, self.settings, self.now_fn(), attestation=True)
            if error:
                raise PricingUnavailable(error)
            if any(profile.get(k) is not True for k in ("workload_equivalent", "performance_assessed", "overheads_assessed", "commitments_assessed")):
                raise PricingUnavailable("Confirm equivalent workload, performance, all dependent costs and commitment effects in the pricing input.")
            if profile.get("currency") != "USD":
                raise PricingUnavailable("Only USD cost comparison is supported.")
            hours = decimal(self.settings.value_of("pricing.hours_per_month"))
            baseline, current = profile.get("current_cost", {}), Decimal(0)
            if not entry.id.startswith("nat-"):
                error = validate_record(baseline, scope, self.settings, self.now_fn())
                if error:
                    raise PricingUnavailable("Current resource-attributed cost: " + error)
                if baseline.get("attribution") != "resource" or baseline.get("currency") != "USD":
                    raise PricingUnavailable("Current cost must be USD, attributed to this resource (CUR or a reconciled billing report), not a service total.")
                start, end = timestamp(baseline.get("period_start")), timestamp(baseline.get("period_end_exclusive"))
                if start is None or end is None or end <= start or end > self.now_fn():
                    raise PricingUnavailable("Current cost needs a completed measurement period with timezone-aware dates.")
                observed_hours = Decimal(str((end - start).total_seconds())) / 3600
                current = decimal(baseline.get("amount_usd")) * hours / observed_hours
            # Costs that continue after migration (for example RI/SP commitments) stay in target spend.
            retained = decimal(profile.get("retained_monthly_cost_usd"))
            target_lines = self._plan(entry, decision, resource, profile, hours)
            for extra in profile.get("additional_target_components", []):
                if not isinstance(extra, dict) or not extra.get("name"):
                    raise PricingUnavailable("Additional target components require name, quantity and query.")
                target_lines.append((extra["name"], decimal(extra.get("monthly_quantity")), extra.get("query")))
            lines, total = [], retained
            if retained:
                lines.append({"name": "Retained source costs / commitments", "amount_exact": str(retained), "source": profile["source"]})
            for name, quantity, query in target_lines:
                if query == "gateway_free":
                    quote = {"amount_exact": "0", "amount_usd": "0.00", "source": GATEWAY_PRICING_URL,
                             "quantity": str(quantity), "unit": "GB", "basis": "Documented gateway endpoint charge; excludes service request/storage costs."}
                else:
                    quote = self.prices.quote(scope["account_id"], scope["region"], query, quantity)
                total += decimal(quote["amount_exact"])
                lines.append({"name": name, **quote})
            if entry.id.startswith("nat-"):
                # Only eligible data-processing spend is avoidable; never claim NAT hourly removal.
                eligible = self._nat_current(decision, profile, hours, scope)
                current = decimal(eligible["amount_exact"])
                baseline = {"source": "AWS Price List x measured eligible bidirectional traffic", **eligible}
            savings = current - total
            pct = savings / current * 100 if current > 0 else Decimal(0)
            min_usd, min_pct = (self.settings.value_of(k) for k in ("pricing.min_monthly_savings_usd", "pricing.min_savings_pct"))
            qualifies = savings > 0 and (min_usd is None or savings >= decimal(min_usd)) and (min_pct is None or pct >= decimal(min_pct))
            return {"status": "estimated", "currency": "USD", "hours_per_month": str(hours),
                    "current_monthly_cost_usd": money(current), "alternative_monthly_cost_usd": money(total),
                    "monthly_savings_usd": money(savings), "savings_pct": money(pct), "recommend": qualifies,
                    "exact": {"current": str(current), "alternative": str(total), "savings": str(savings)},
                    "current_cost_evidence": baseline, "target_components": lines,
                    "scope": "eligible NAT data-processing only" if entry.id.startswith("nat-") else "resource workload and assessed dependent costs",
                    "assumptions": [f"Normalize to {hours} hours per month using the stated observed workload.",
                                    "Estimates are not a bill or a migration guarantee.",
                                    "Target uses public on-demand USD rates, excludes tax/free tier/private discounts.",
                                    "Benchmark equivalence and all dependent costs are explicitly assessed by the workload owner.",
                                    "NAT hourly charges and unrelated traffic remain." if entry.id.startswith("nat-") else
                                    "Retained source costs and commitments are included in the alternative total."],
                    "input_source": profile["source"], "input_observed_at": profile["observed_at"]}
        except Exception as exc:
            return {"status": "insufficient_data", "reason": f"{type(exc).__name__}: {exc}",
                    "recommend": False, "missing_inputs": "See docs/recommendations.md for the resource-scoped pricing schema."}

    @staticmethod
    def _need(profile, key):
        result = decimal(profile.get(key))
        if result <= 0:
            raise PricingUnavailable(f"{key} must be positive.")
        return result

    @staticmethod
    def _query(profile, key, service, unit, required=None, usage_suffix=None):
        query = profile.get("rates", {}).get(key, {"service_code": service, "unit": unit, "filters": dict(required or {})})
        if not isinstance(query, dict) or query.get("service_code") != service or query.get("unit") != unit:
            raise PricingUnavailable(f"rates.{key} must select an exact {service} product priced in {unit}.")
        for field, val in (required or {}).items():
            if query.get("filters", {}).get(field) != val:
                raise PricingUnavailable(f"rates.{key} must match {field}={val} for this workload.")
        if usage_suffix:
            query = {**query, "usage_type_suffix": usage_suffix}
        return query

    def _plan(self, entry, decision, resource, profile, hours):
        eid = entry.id
        if eid.startswith("nat-"):
            return [("Gateway endpoint charges", Decimal(0), "gateway_free")]
        if eid == "ec2-short-event-driven-to-lambda":
            monthly = self._need(profile, "monthly_invocations")
            memory = self._need(profile, "lambda_memory_mb")
            duration = self._need(profile, "lambda_billed_duration_ms")
            minimum = max(Decimal(128), decimal(decision["evidence"]["ec2.peak_memory_mib"]["value"]))
            if not minimum <= memory <= 10240 or memory != memory.to_integral_value():
                raise PricingUnavailable("Lambda memory must cover measured peak memory and be a valid whole-MB setting.")
            if duration > 900000:
                raise PricingUnavailable("Standard Lambda billed duration exceeds 900 seconds.")
            arch = profile.get("target_architecture")
            if arch not in ("x86_64", "arm64") or profile.get("architecture_assessed") is not True:
                raise PricingUnavailable("Specify and assess the target Lambda architecture.")
            return [("Lambda requests", monthly, self._query(profile, "lambda_requests", "AWSLambda", "Requests", {"group": "AWS-Lambda-Requests" if arch == "x86_64" else "AWS-Lambda-Requests-ARM"})),
                    ("Lambda duration", monthly * duration.to_integral_value(rounding="ROUND_CEILING") / 1000 * memory / 1024,
                     self._query(profile, "lambda_duration", "AWSLambda", "Lambda-GB-Second", {"group": "AWS-Lambda-Duration" if arch == "x86_64" else "AWS-Lambda-Duration-ARM"}))]
        if eid in ("ec2-containerized-to-fargate", "lambda-steady-high-volume-to-fargate"):
            if profile.get("target_os_family") not in ("LINUX", "WINDOWS"):
                raise PricingUnavailable("Specify the Fargate operating system.")
            arch = profile.get("target_architecture")
            if arch not in ("x86_64", "arm64") or (profile["target_os_family"] == "WINDOWS" and arch != "x86_64"):
                raise PricingUnavailable("Unsupported Fargate architecture / OS combination.")
            if eid.startswith("ec2-"):
                size = decision["fargate_size"]
                if profile["target_os_family"] != decision["evidence"]["ec2.target_os_family"]["value"] or arch != resource.get("architecture"):
                    raise PricingUnavailable("Fargate OS and architecture must match the evaluated workload.")
            else:
                size = fargate_size(float(self._need(profile, "target_peak_vcpu")), float(self._need(profile, "target_peak_memory_mib")),
                                    profile["target_os_family"], self.settings.value_of("ec2_to_fargate.headroom_pct"))
            if not size:
                raise PricingUnavailable("No valid Fargate task size satisfies the measured target workload.")
            if size["requires_linux_platform_1_4"] and profile.get("platform_version_assessed") is not True:
                raise PricingUnavailable("This Fargate size requires verified platform-version availability.")
            task_hours = self._need(profile, "monthly_billable_task_hours")
            # Input includes image-pull time, task count and per-task minimum billing duration.
            if profile.get("task_billing_assessed") is not True:
                raise PricingUnavailable("Task hours must include task count, image pull/startup and billing minima.")
            osname = "Windows" if profile["target_os_family"] == "WINDOWS" else "Linux"
            # Linux/x86 products omit operatingSystem and cpuArchitecture. Their usage types
            # distinguish them from ARM/Windows. Both CPU and RAM rate dimensions use "hours".
            prefix = "Fargate-Windows-" if osname == "Windows" else "Fargate-ARM-" if arch == "arm64" else "Fargate-"
            lines = [("Fargate CPU", task_hours * decimal(size["vcpu"]), self._query(profile, "fargate_cpu", "AmazonECS", "hours", usage_suffix=prefix + "vCPU-Hours:perCPU")),
                     ("Fargate memory", task_hours * decimal(size["memory_mib"]) / 1024, self._query(profile, "fargate_memory", "AmazonECS", "hours", usage_suffix=prefix + "GB-Hours"))]
            if osname == "Windows":
                lines.append(("Fargate Windows license", task_hours * decimal(size["vcpu"]), self._query(profile, "fargate_windows_license", "AmazonECS", "hours", usage_suffix="Fargate-Windows-OS-Hours:perCPU")))
            return lines
        if eid.startswith("rds-"):
            ev = decision["evidence"]
            capacity = ev["rds.workload_capacity_estimate"]["value"]
            mapping = ev["rds.aurora_version_mapping"]["value"]
            nodes = self._need(profile, "aurora_node_count")
            if nodes != nodes.to_integral_value() or (resource.get("multi_az") and nodes < 2):
                raise PricingUnavailable("The Aurora topology must preserve assessed availability requirements.")
            engine = "Aurora MySQL" if mapping["target_engine"] == "aurora-mysql" else "Aurora PostgreSQL"
            io_mode = profile.get("aurora_io_mode")
            if io_mode not in ("standard", "io_optimized"):
                raise PricingUnavailable("Specify standard or io_optimized Aurora billing mode.")
            acu_suffix = "Aurora:ServerlessV2Usage" if io_mode == "standard" else "Aurora:ServerlessV2IOOptimizedUsage"
            storage_suffix = "Aurora:StorageUsage" if io_mode == "standard" else "Aurora:IO-OptimizedStorageUsage"
            storage_engine = "Any" if engine == "Aurora MySQL" and io_mode == "standard" else engine
            lines = [("Aurora Serverless v2 compute", decimal(capacity["average_acu"]) * hours * nodes,
                      self._query(profile, "aurora_acu", "AmazonRDS", "ACU-Hr", {"databaseEngine": engine}, acu_suffix)),
                     ("Aurora storage", self._need(profile, "aurora_storage_gb_month"),
                      self._query(profile, "aurora_storage", "AmazonRDS", "GB-Mo", {"databaseEngine": storage_engine}, storage_suffix))]
            if io_mode == "standard":
                lines.append(("Aurora I/O", decimal(profile.get("aurora_monthly_io_requests")),
                              self._query(profile, "aurora_io", "AmazonRDS", "IOs", {"databaseEngine": "Any" if engine == "Aurora MySQL" else engine}, "Aurora:StorageIOUsage")))
            if profile.get("io_mode_rates_assessed") is not True:
                raise PricingUnavailable("Confirm compute/storage SKUs correspond to the selected Aurora I/O mode.")
            return lines
        raise PricingUnavailable("No equivalent-workload pricing plan is defined for this catalog entry.")

    def _nat_current(self, decision, profile, hours, scope):
        subject = decision["subject"]
        record = decision["evidence"][f"nat.{subject}.same_region_bytes"]
        start, end = timestamp(record.get("period_start")), timestamp(record.get("period_end_exclusive"))
        if start is None or end is None or end <= start or end > self.now_fn():
            raise PricingUnavailable("Eligible NAT bytes need a completed observed time window.")
        if record.get("traffic_scope") != "uncovered_route_tables" or record.get("bidirectional") is not True:
            raise PricingUnavailable("NAT traffic must be deduplicated bidirectional eligible bytes from uncovered route tables only.")
        gb = decimal(record["value"]) / (1024 ** 3) * hours / (Decimal(str((end - start).total_seconds())) / 3600)
        query = self._query(profile, "nat_data_processing", "AmazonEC2", "GB", {"productFamily": "NAT Gateway"}, "NatGateway-Bytes")
        return self.prices.quote(scope["account_id"], scope["region"], query, gb)
