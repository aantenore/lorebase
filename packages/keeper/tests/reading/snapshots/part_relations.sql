SELECT r.kind_id, r.key, r.title, r.values, r.derived, r.object_hash, r.lineage, r.updated_revision, r.updated_at, r.key AS s0 
FROM mat_records AS r 
WHERE r.domain_id = %(param_1)s::VARCHAR AND r.branch = %(param_2)s::VARCHAR AND r.kind_id = %(param_3)s::VARCHAR AND (EXISTS (SELECT %(param_4)s::INTEGER AS anon_1 
FROM mat_links AS mat_links_1 JOIN mat_records AS mat_records_1 ON mat_records_1.domain_id = %(param_5)s::VARCHAR AND mat_records_1.branch = %(param_6)s::VARCHAR AND mat_records_1.kind_id = %(param_7)s::VARCHAR AND mat_records_1.key = mat_links_1.to_key 
WHERE mat_links_1.domain_id = %(param_8)s::VARCHAR AND mat_links_1.branch = %(param_9)s::VARCHAR AND mat_links_1.relation_id = %(param_10)s::VARCHAR AND mat_links_1.from_key = r.key AND (mat_records_1.values ->> %(param_11)s::VARCHAR) IN (%(param_12)s::VARCHAR, %(param_13)s::VARCHAR))) AND (SELECT count(*) AS count_1 
FROM mat_links AS mat_links_2 
WHERE mat_links_2.domain_id = %(param_14)s::VARCHAR AND mat_links_2.branch = %(param_15)s::VARCHAR AND mat_links_2.relation_id = %(param_16)s::VARCHAR AND mat_links_2.from_key = r.key) >= %(param_17)s::INTEGER AND NOT (r.search @@ websearch_to_tsquery(CAST(%(param_18)s::VARCHAR AS REGCONFIG), %(param_19)s::VARCHAR)) ORDER BY r.key ASC 
 LIMIT %(param_20)s::INTEGER
-- params
{
  "param_1": "supply",
  "param_10": "rel_01KEZF7C00F4CJ0YEKSMQT6PC0",
  "param_11": "att_01KEZF7C00Z60FEN5RNWEENNX2",
  "param_12": "IT",
  "param_13": "DE",
  "param_14": "supply",
  "param_15": "main",
  "param_16": "rel_01KEZF7C00F4CJ0YEKSMQT6PC0",
  "param_17": 2,
  "param_18": "simple",
  "param_19": "obsolete seal",
  "param_2": "main",
  "param_20": 51,
  "param_3": "knd_01KEZF7C00WBDCAD84MRED7WQV",
  "param_4": 1,
  "param_5": "supply",
  "param_6": "main",
  "param_7": "knd_01KEZF7C007ZDX8SK1DZP7MXFF",
  "param_8": "supply",
  "param_9": "main"
}
