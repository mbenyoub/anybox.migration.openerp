import csv
import psycopg2.extras
import logging
import os
from os.path import basename, join

HERE = os.path.dirname(__file__)
logging.basicConfig(level=logging.DEBUG)
LOG = logging.getLogger(basename(__file__))


class CSVProcessor(object):
    """ Take a csv file, process it with the mapping
    and output a new csv file
    """
    def __init__(self, mapping, fields2update=None):

        self.fields2update = fields2update
        self.mapping = mapping
        self.target_columns = {}
        self.writers = {}
        self.updated_values = {}
        self.deferred_updates = []

    def get_target_columns(self, filepaths):
        """ Compute target columns with source columns + mapping
        """
        if self.target_columns:
            return self.target_columns
        for filepath in filepaths:
            source_table = basename(filepath).rsplit('.', 1)[0]
            with open(filepath) as f:
                source_columns = csv.reader(f).next()
            for source_column in source_columns + ['_']:
                mapping = self.mapping.get_targets('%s.%s' % (source_table, source_column))
                # no mapping found, we warn the user
                if mapping in (None, '__copy__'):
                    origin = source_table + '.' + source_column
                    if source_column != '_':
                        LOG.warn('No mapping definition found for column %s', origin)
                    continue
                elif mapping in (False, '__forget__'):
                    continue
                else:
                    for target in mapping:
                        t, c = target.split('.')
                        self.target_columns.setdefault(t, set()).add(c)

        self.target_columns = {k: sorted([c for c in v if c != '_'])
                               for k, v in self.target_columns.items()}
        return self.target_columns

    def process(self, source_dir, source_filenames, target_dir,
                target_connection=None, existing_records=None, fields2update=None):
        """ The main processing method
        """
        # compute the target columns
        filepaths = [join(source_dir, source_filename) for source_filename in source_filenames]
        self.target_columns = self.get_target_columns(filepaths)
        # load discriminator values for target tables
        # TODO
        # open target files for writing
        self.target_files = {
            table: open(join(target_dir, table + '.out.csv'), 'ab')
            for table in self.target_columns
        }
        # create csv writers
        self.writers = {t: csv.DictWriter(f, self.target_columns[t], delimiter=',')
                        for t, f in self.target_files.items()}
        # write csv headers once
        for writer in self.writers.values():
            writer.writeheader()
        # process csv files
        for source_filename in source_filenames:
            source_filepath = join(source_dir, source_filename)
            self.process_one(source_filepath, target_connection, existing_records, fields2update)
        for target_file in self.target_files.values():
            target_file.close()

    def process_one(self, source_filepath,
                    target_connection=None, existing_records=None, fields2update=None):
        """ Process one csv file
        """
        source_table = basename(source_filepath).rsplit('.', 1)[0]
        with open(source_filepath, 'rb') as source_csv:
            reader = csv.DictReader(source_csv, delimiter=',')
            # process each csv line
            for source_row in reader:
                target_rows = {}
                # process each column (also handle '_' as a possible new column)
                source_row.update({'_': None})
                for source_column in source_row:
                    mapping = self.mapping.get_targets(source_table + '.' + source_column)
                    if mapping is None:
                        continue
                    # we found a mapping, use it
                    for target_column, function in mapping.items():
                        target_table, target_column = target_column.split('.')
                        target_rows.setdefault(target_table, {})
                        if target_column == '_':
                            continue
                        if function in (None, '__copy__'):
                            # mapping is None: use identity
                            target_rows[target_table][target_column] = source_row[source_column]
                        elif function in (False, '__forget__'):
                            # mapping is False: remove the target column
                            del target_rows[target_table][target_column]
                        else:
                            # mapping is supposed to be a function
                            result = function(source_row, target_rows)
                            target_rows[target_table][target_column] = result
                # postprocess the target lines and write them to csv
                for table, target_row in target_rows.items():
                    if any(target_row.values()):
                        # offset the foreign keys of the line, except for those in existing target records
                        for key, value in target_row.items():
                            existing = existing_records.get(fields2update.get(table + '.' + key, 'x'), [])
                            if (fields2update and (table + '.' + key) in fields2update
                                    and target_row.get(key, '')):
                                if value and int(value) not in [v['id'] for v in existing]:
                                    last_id = self.mapping.last_id.get( fields2update[table + '.' + key], 0)
                                    target_row[key] = int(target_row[key]) + last_id
                                # if the foreign key points to an existing records, replace with the target id
                                if value and int(value) in [v['id'] for v in existing]:
                                    for i, nt in enumerate(existing):
                                        if value in {nt[k] for k in nt.keys() if k != 'id'}:
                                            target_row[key] = existing[i]['id']
                                            break

                        # offset the id of the line
                        if (fields2update and 'id' in target_row and table in fields2update.itervalues()):
                            last_id = self.mapping.last_id.get(table, 0)
                            target_row['id'] = int(target_row['id']) + last_id
                        # create a deferred update for existing data in the target
                        discriminators = self.mapping.discriminators.get(table)
                        if discriminators:
                            values = {target_row[d] for d in discriminators}
                            if values in [{nt[k] for k in nt.keys() if k != 'id'} for nt in existing_records[table]]:
                                columns = ', '.join(target_row.keys())  # FIXME sauf id
                                values = ', '.join(['%s' for i in target_row])
                                args = tuple( (i if i != '' else None for i in target_row.values() + [target_row[k] for k in discriminators]))
                                AND = 'AND '.join(
                                    ['='.join((k, '%s')) for k in discriminators])
                                update = ('UPDATE %s SET (%s)=(%s) WHERE %s' % (table, columns, values, AND), args)
                                self.deferred_updates.append(update)
                                continue
                        # write the csv line
                        self.writers[table].writerow(target_row)

    def check_record(self, target_connection, table, target_row):
        """ Method to check if one record has an equivalent in the
        targeted db"""

        c = target_connection.cursor(cursor_factory=psycopg2.extras.DictCursor)
        if table in self.mapping.discriminators:
            if len(self.mapping.discriminators[table]) > 1:
                raise NotImplementedError('multiple discriminators are not yet supported')
            discriminator = self.mapping.discriminators[table][0]
        try:
            query = ("SELECT * FROM %s WHERE %s = '%s';"
                     % (table, discriminator, target_row[discriminator]))
            c.execute(query)
            record_cible = c.fetchone()
        except:
            record_cible = None
        if record_cible:
            if record_cible['id'] != target_row['id']:
                target_row['id'] = record_cible['id']
            elif record_cible['id'] == target_row['id']:
                pass
        else:
            if table in self.mapping.last_id:
                if 'id' in target_row:
                    target_row['id'] = str(self.mapping.last_id[table] + int(target_row['id']))
                if self.fields2update and table in self.fields2update:
                    for tbl, field in self.fields2update[table]:
                        self.updated_values[tbl] = {}
            self.updated_values[tbl][field] = target_row['id']
        if table in self.updated_values:
            raise NotImplementedError
        else:
            # Il s'agit d'une table de jointure ! Comment gerer ca ?
            raise NotImplementedError
        if table in self.updated_values:
            # La, on update
            raise NotImplementedError
        else:
            raise NotImplementedError
