import type { ReactNode } from 'react';
import { useCollection } from '@cloudscape-design/collection-hooks';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Header from '@cloudscape-design/components/header';
import Pagination from '@cloudscape-design/components/pagination';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Table, { type TableProps } from '@cloudscape-design/components/table';
import TextFilter from '@cloudscape-design/components/text-filter';

interface ResourceTableProps<T> {
  title: string;
  description?: ReactNode;
  items: readonly T[];
  columns: TableProps.ColumnDefinition<T>[];
  trackBy: string | ((item: T) => string);
  loading?: boolean;
  actions?: ReactNode;
  empty: ReactNode;
  filterPlaceholder?: string;
  searchText?: (item: T) => string;
  variant?: TableProps.Variant;
  pageSize?: number;
  sortingField?: string;
  descending?: boolean;
  showCounter?: boolean;
}

/** Shared Cloudscape collection behavior; records always come from the caller. */
export function ResourceTable<T>({
  title, description, items: allItems, columns, trackBy, loading = false,
  actions, empty, filterPlaceholder, searchText, variant = 'container',
  pageSize = 10, sortingField, descending = false, showCounter = true,
}: ResourceTableProps<T>) {
  const collection = useCollection(allItems, {
    filtering: {
      empty: <Box textAlign="center" padding="l">{empty}</Box>,
      noMatch: <Box textAlign="center" padding="l"><SpaceBetween size="s">
        <Box variant="strong">No matches</Box>
        <Box color="text-body-secondary">Try another search or clear the filter.</Box>
        <Button onClick={() => collection.actions.setFiltering('')}>Clear filter</Button>
      </SpaceBetween></Box>,
      ...(searchText ? { filteringFunction: (item: T, text: string) =>
        searchText(item).toLocaleLowerCase().includes(text.toLocaleLowerCase()) } : {}),
    },
    sorting: { ...(sortingField ? { defaultState: { sortingColumn: { sortingField }, isDescending: descending } } : {}) },
    pagination: { pageSize },
  });
  return <Table<T>
    {...collection.collectionProps}
    variant={variant} trackBy={trackBy} items={collection.items} columnDefinitions={columns}
    loading={loading} loadingText={`Loading ${title.toLowerCase()}`}
    wrapLines
    header={<Header variant="h2" counter={loading || !showCounter ? undefined : `(${allItems.length})`}
      description={description} actions={actions}>{title}</Header>}
    filter={<TextFilter {...collection.filterProps}
      filteringPlaceholder={filterPlaceholder || `Find ${title.toLowerCase()}`}
      filteringAriaLabel={filterPlaceholder || `Find ${title.toLowerCase()}`}
      countText={`${collection.filteredItemsCount ?? allItems.length} matches`} />}
    pagination={<Pagination {...collection.paginationProps}
      ariaLabels={{ nextPageLabel: 'Next page', previousPageLabel: 'Previous page', pageLabel: page => `Page ${page}` }} />}
    ariaLabels={{ tableLabel: title }}
  />;
}
