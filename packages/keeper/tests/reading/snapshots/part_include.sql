SELECT ranked.near_key, ranked.far_key, ranked.link_values, ranked.kind_id, ranked.key, ranked.title, ranked.values, ranked.derived, ranked.object_hash, ranked.lineage, ranked.updated_revision, ranked.updated_at 
FROM (SELECT mat_links_1.from_key AS near_key, mat_links_1.to_key AS far_key, mat_links_1.values AS link_values, far.kind_id AS kind_id, far.key AS key, far.title AS title, far.values AS values, far.derived AS derived, far.object_hash AS object_hash, far.lineage AS lineage, far.updated_revision AS updated_revision, far.updated_at AS updated_at, row_number() OVER (PARTITION BY mat_links_1.from_key ORDER BY mat_links_1.to_key) AS position 
FROM mat_links AS mat_links_1 JOIN mat_records AS far ON far.domain_id = %(param_1)s::VARCHAR AND far.branch = %(param_2)s::VARCHAR AND far.kind_id = %(param_3)s::VARCHAR AND far.key = mat_links_1.to_key 
WHERE mat_links_1.domain_id = %(param_4)s::VARCHAR AND mat_links_1.branch = %(param_5)s::VARCHAR AND mat_links_1.relation_id = %(param_6)s::VARCHAR AND mat_links_1.from_key IN (%(param_7)s::VARCHAR, %(param_8)s::VARCHAR)) AS ranked 
WHERE ranked.position <= %(param_9)s::INTEGER ORDER BY ranked.near_key, ranked.far_key
-- params
{
  "param_1": "supply",
  "param_2": "main",
  "param_3": "knd_01KEZF7C007ZDX8SK1DZP7MXFF",
  "param_4": "supply",
  "param_5": "main",
  "param_6": "rel_01KEZF7C00F4CJ0YEKSMQT6PC0",
  "param_7": "P-1",
  "param_8": "P-2",
  "param_9": 3
}
