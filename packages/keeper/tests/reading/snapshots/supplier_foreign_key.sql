SELECT r.kind_id, r.key, r.title, r.values, r.derived, r.object_hash, r.lineage, r.updated_revision, r.updated_at, CAST(r.values ->> %(param_1)s::VARCHAR AS DATE) IS NULL AS s0, CAST(r.values ->> %(param_1)s::VARCHAR AS DATE) AS s1, r.key AS s2 
FROM mat_records AS r 
WHERE r.domain_id = %(param_2)s::VARCHAR AND r.branch = %(param_3)s::VARCHAR AND r.kind_id = %(param_4)s::VARCHAR AND ((EXISTS (SELECT %(param_5)s::INTEGER AS anon_1 
FROM mat_records AS mat_records_1 JOIN mat_records AS mat_records_2 ON mat_records_2.domain_id = %(param_6)s::VARCHAR AND mat_records_2.branch = %(param_7)s::VARCHAR AND mat_records_2.kind_id = %(param_8)s::VARCHAR AND mat_records_2.key = mat_records_1.key 
WHERE mat_records_1.domain_id = %(param_9)s::VARCHAR AND mat_records_1.branch = %(param_10)s::VARCHAR AND mat_records_1.kind_id = %(param_11)s::VARCHAR AND (mat_records_1.values ? %(values_1)s::VARCHAR) AND (mat_records_1.values ->> %(param_12)s::VARCHAR) = r.key AND CAST(mat_records_2.values ->> %(param_13)s::VARCHAR AS DATE) < %(param_14)s::DATE)) OR CAST(r.values ->> %(param_15)s::VARCHAR AS DATE) >= %(param_16)s::DATE) ORDER BY CAST(r.values ->> %(param_1)s::VARCHAR AS DATE) IS NULL ASC, CAST(r.values ->> %(param_1)s::VARCHAR AS DATE) ASC, r.key ASC 
 LIMIT %(param_17)s::INTEGER
-- params
{
  "param_1": "att_01KEZF7C00N37SV7HXQT83TCAG",
  "param_10": "main",
  "param_11": "knd_01KEZF7C00M11T0RH8CAVBW897",
  "param_12": "att_01KEZF7C007AC0DF84HCQRX9ZT",
  "param_13": "att_01KEZF7C00537ZFFD1EAF7VVNY",
  "param_14": "date:2026-01-01",
  "param_15": "att_01KEZF7C00N37SV7HXQT83TCAG",
  "param_16": "date:2024-01-01",
  "param_17": 51,
  "param_2": "supply",
  "param_3": "main",
  "param_4": "knd_01KEZF7C007ZDX8SK1DZP7MXFF",
  "param_5": 1,
  "param_6": "supply",
  "param_7": "main",
  "param_8": "knd_01KEZF7C00M11T0RH8CAVBW897",
  "param_9": "supply",
  "values_1": "att_01KEZF7C007AC0DF84HCQRX9ZT"
}
