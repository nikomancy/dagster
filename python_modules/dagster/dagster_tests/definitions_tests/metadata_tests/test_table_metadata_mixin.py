from dagster import AssetKey, AssetMaterialization, TableColumn, TableSchema
from dagster._core.definitions.metadata import TableMetadataMixin
from dagster._core.definitions.metadata.table import (
    TableColumnDep,
    TableColumnLineage,
)


def test_table_metadata_mixin():
    column_schema = TableSchema(columns=[TableColumn("foo", "str")])
    table_metadata = TableMetadataMixin(column_schema=column_schema)

    dict_table_metadata = dict(table_metadata)
    assert dict_table_metadata == {"dagster/column_schema": column_schema}
    assert isinstance(dict_table_metadata["dagster/column_schema"], TableSchema)
    AssetMaterialization(asset_key="a", metadata=dict_table_metadata)

    splat_table_metadata = {**table_metadata}
    assert splat_table_metadata == {"dagster/column_schema": column_schema}
    assert isinstance(splat_table_metadata["dagster/column_schema"], TableSchema)
    AssetMaterialization(asset_key="a", metadata=splat_table_metadata)

    assert dict(TableMetadataMixin()) == {}
    assert TableMetadataMixin.extract(dict(TableMetadataMixin())) == TableMetadataMixin()


def test_column_specs() -> None:
    expected_column_lineage = TableColumnLineage(
        {
            "column": [
                TableColumnDep(
                    asset_key=AssetKey("upstream"),
                    column_name="upstream_column",
                )
            ]
        }
    )
    expected_metadata = {"dagster/column_lineage": expected_column_lineage}

    table_metadata = TableMetadataMixin(column_lineage=expected_column_lineage)

    dict_table_metadata = dict(table_metadata)
    assert dict_table_metadata == expected_metadata

    materialization = AssetMaterialization(asset_key="foo", metadata=dict_table_metadata)
    extracted_table_metadata_entries = TableMetadataMixin.extract(materialization.metadata)
    assert extracted_table_metadata_entries.column_lineage == expected_column_lineage

    splat_table_metadata_entries = {**table_metadata}
    assert splat_table_metadata_entries == expected_metadata

    materialization = AssetMaterialization(asset_key="foo", metadata=splat_table_metadata_entries)
    extracted_table_metadata_entries = TableMetadataMixin.extract(materialization.metadata)
    assert extracted_table_metadata_entries.column_lineage == expected_column_lineage
