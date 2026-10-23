SELECT r.kind_id, r.key, r.title, r.values, r.derived, r.object_hash, lb_lineage(r.domain_id, r.lineage) AS lineage, r.updated_revision, r.updated_at, CAST(r.values ->> %(param_1)s::VARCHAR AS NUMERIC) IS NULL AS s0, CAST(r.values ->> %(param_1)s::VARCHAR AS NUMERIC) AS s1, r.key AS s2 
FROM mat_records AS r 
WHERE r.domain_id = %(param_2)s::VARCHAR AND r.branch = %(param_3)s::VARCHAR AND r.kind_id = %(param_4)s::VARCHAR AND (r.values @> %(param_5)s::JSONB) AND CAST(r.values ->> %(param_6)s::VARCHAR AS NUMERIC) BETWEEN %(param_7)s AND %(param_8)s AND ((r.values ->> %(param_9)s::VARCHAR) LIKE %(param_10)s::VARCHAR || '%%' ESCAPE '/') AND NOT ((r.values ? %(values_1)s::VARCHAR)) AND ((CAST(r.values ->> %(param_1)s::VARCHAR AS NUMERIC) IS NULL) > %(param_11)s OR (CAST(r.values ->> %(param_1)s::VARCHAR AS NUMERIC) IS NULL) = %(param_11)s AND CAST(r.values ->> %(param_1)s::VARCHAR AS NUMERIC) < %(param_12)s OR (CAST(r.values ->> %(param_1)s::VARCHAR AS NUMERIC) IS NULL) = %(param_11)s AND CAST(r.values ->> %(param_1)s::VARCHAR AS NUMERIC) = %(param_12)s AND r.key > %(param_13)s::VARCHAR) ORDER BY CAST(r.values ->> %(param_1)s::VARCHAR AS NUMERIC) IS NULL ASC, CAST(r.values ->> %(param_1)s::VARCHAR AS NUMERIC) DESC, r.key ASC 
 LIMIT %(param_14)s::INTEGER
-- params
{
  "param_1": "att_01KEZF7C00NK9JAYQ84CSTKF4C",
  "param_10": "Kav/%/_",
  "param_11": false,
  "param_12": "Decimal:38",
  "param_13": "S-1002",
  "param_14": 21,
  "param_2": "supply",
  "param_3": "main",
  "param_4": "knd_01KEZF7C007ZDX8SK1DZP7MXFF",
  "param_5": {
    "att_01KEZF7C00Z60FEN5RNWEENNX2": "PL"
  },
  "param_6": "att_01KEZF7C00NK9JAYQ84CSTKF4C",
  "param_7": 10,
  "param_8": 80,
  "param_9": "att_01KEZF7C00H73NVMMGX4KPCH9W",
  "values_1": "att_01KEZF7C00NAFHVPJ4YPMXSH3H"
}
