"""AWS-first catalog candidates: not compatibility or savings recommendations."""


OWNER_QUESTIONS = {
    "workload.event_driven": "Does each job respond to an event/manual request, rather than run continuously? Leave unknown if not established.",
    "workload.max_event_duration_seconds": "What is the measured or documented maximum complete task duration, including input/output? Do not guess.",
    "workload.lambda_invocation_type": "For the proposed Lambda design, should the caller wait (synchronous), submit and return (asynchronous), or use a queue/stream event mapping?",
    "workload.max_request_payload_mb": "What is the largest proposed invocation request size? An S3 bucket/key reference is not the stored file itself.",
    "workload.max_response_payload_mb": "What is the largest proposed invocation response size? Leave unknown if not designed or measured.",
    "workload.containerizable": "Has this application been verified to run in a container?",
}


def assessment_candidates(candidates, catalog):
    entries = {e.id: e for e in catalog.approved_entries()}
    result = []
    for c in candidates:
        entry = entries.get(c.get("alternative_id"))
        details = c.get("details", {})
        if not entry or details.get("errors") or details.get("status") == "partial":
            continue
        for report in details.get("reports", []):
            d = report.get("decision", {})
            if report.get("status") != "needs_information" or d.get("configuration_issues") or d.get("verdict") not in ("insufficient_data", "compatible"):
                continue
            facts, questions, gaps = {}, [], []
            for key, observation in d.get("evidence", {}).items():
                req = entry.evidence_requirement(key)
                if req and req.origin == "collected" and observation.get("status") == "available":
                    facts[key] = observation.get("value")
            if not facts:
                continue
            for reason in d.get("reasons", []):
                key = reason.get("evidence_key", "")
                req = entry.evidence_requirement(key)
                if req and req.origin == "attestation" and not key.startswith("nat."):
                    q = {"field": key, "question": OWNER_QUESTIONS.get(key, req.description), "type": req.type,
                         "answer_path": f'answers["{key}"]'}
                    if req.values:
                        q["allowed_values"] = list(req.values)
                    if req.unit:
                        q["unit"] = req.unit
                    if q not in questions:
                        questions.append(q)
                elif key == "collection" and "workload_assessment" in reason.get("message", ""):
                    q = {"field": "requirements_assessed", "type": "boolean",
                         "question": "Have state, storage, runtime, networking, security and availability requirements been assessed for this candidate?",
                         "answer_path": 'answers["requirements_assessed"]'}
                    if q not in questions:
                        questions.append(q)
                else:
                    gaps.append({"field": key, "message": reason.get("message", "Evidence is incomplete.")})
            result.append({"service": entry.alternative_name, "alternative_id": entry.id,
                           **{k: d[k] for k in ("account_id", "region", "resource_id", "subject") if d.get(k) is not None},
                           "status": "candidate_for_assessment", "validation": "not_validated",
                           "reason": "AWS inventory found the source resource. This catalog alternative needs workload assessment before selection.",
                           "automatic_facts": facts, "questions": questions, "evidence_gaps": gaps,
                           "estimated_monthly_savings_usd": None})
    return result
