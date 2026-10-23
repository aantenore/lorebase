SELECT r.kind_id, r.key, r.title, r.values, r.derived, r.object_hash, lb_lineage(r.domain_id, r.lineage) AS lineage, r.updated_revision, r.updated_at, CASE WHEN (r.kind_id = %(param_1)s::VARCHAR) THEN r.values ->> %(param_2)s::VARCHAR WHEN (r.kind_id = %(param_3)s::VARCHAR) THEN r.values ->> %(param_4)s::VARCHAR END IS NULL AS s0, CASE WHEN (r.kind_id = %(param_1)s::VARCHAR) THEN r.values ->> %(param_2)s::VARCHAR WHEN (r.kind_id = %(param_3)s::VARCHAR) THEN r.values ->> %(param_4)s::VARCHAR END AS s1, r.kind_id AS s2, r.key AS s3 
FROM mat_records AS r 
WHERE r.domain_id = %(param_5)s::VARCHAR AND r.branch = %(param_6)s::VARCHAR AND r.kind_id IN (%(param_7)s::VARCHAR, %(param_8)s::VARCHAR) AND CASE WHEN (r.kind_id = %(param_9)s::VARCHAR) THEN r.values ->> %(param_10)s::VARCHAR WHEN (r.kind_id = %(param_11)s::VARCHAR) THEN r.values ->> %(param_12)s::VARCHAR END != %(param_13)s::VARCHAR ORDER BY CASE WHEN (r.kind_id = %(param_1)s::VARCHAR) THEN r.values ->> %(param_2)s::VARCHAR WHEN (r.kind_id = %(param_3)s::VARCHAR) THEN r.values ->> %(param_4)s::VARCHAR END IS NULL ASC, CASE WHEN (r.kind_id = %(param_1)s::VARCHAR) THEN r.values ->> %(param_2)s::VARCHAR WHEN (r.kind_id = %(param_3)s::VARCHAR) THEN r.values ->> %(param_4)s::VARCHAR END ASC, r.kind_id ASC, r.key ASC 
 LIMIT %(param_14)s::INTEGER
-- params
{
  "param_1": "knd_01KEZF7C007ZDX8SK1DZP7MXFF",
  "param_10": "att_01KEZF7C00Z60FEN5RNWEENNX2",
  "param_11": "knd_01KEZF7C00PHQ0PFYPZECGNE96",
  "param_12": "att_01KEZF7C00EPAKJ5RH34T1XBB0",
  "param_13": "IT",
  "param_14": 51,
  "param_2": "att_01KEZF7C009AT17WTRXEX9MT8Y",
  "param_3": "knd_01KEZF7C00PHQ0PFYPZECGNE96",
  "param_4": "att_01KEZF7C00H48J07NBG96J0Q2S",
  "param_5": "supply",
  "param_6": "main",
  "param_7": "knd_01KEZF7C007ZDX8SK1DZP7MXFF",
  "param_8": "knd_01KEZF7C00PHQ0PFYPZECGNE96",
  "param_9": "knd_01KEZF7C007ZDX8SK1DZP7MXFF"
}
