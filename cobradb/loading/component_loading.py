# -*- coding: utf-8 -*-

from cobradb import settings, base
from cobradb.base import *
from cobradb.components import Gene, Protein
from cobradb.util import scrub_gene_id, get_or_create_data_source, get_or_create, timing
from cobradb.loading import AlreadyLoadedError

import sys, os, math, re
from os.path import basename
from warnings import warn
from sqlalchemy import text, or_, and_, func
import logging
import six


class BadGenomeError(Exception):
    pass


def _load_gb_file(genbank_filepath):
    """Load the Genbank file.

    Arguments
    ---------

    genbank_filepath: The path to the genbank file.

    """
    # imports
    from Bio import SeqIO

    # load the genbank file
    logging.debug('Loading file: %s' % genbank_filepath)
    try:
        gb_file = SeqIO.read(genbank_filepath, 'gb')
    except IOError:
        raise BadGenomeError("File '%s' not found" % genbank_filepath)
    except Exception as e:
        raise BadGenomeError('BioPython failed to parse %s with error "%s"' %
                             (genbank_filepath, e.message))
    return gb_file


def get_genbank_accessions(genbank_filepath, fast=False):
    """Load the file and return the NCBI Accession and Assembly IDs (if available).

    Returns a dictionary of accessions with keys: 'ncbi_accession',
    'ncbi_assembly', 'ncbi_bioproject'.

    Arguments
    ---------

    genbank_filepath: The path to the genbank file.

    fast: If True, then only look in the first 100 lines. Faster because we do
    not load the whole file.

    """
    out = {'ncbi_assembly': None,
           'ncbi_accession': None,
           'ncbi_bioproject': None}

    if fast:
        # try to find the BioProject ID in the first 100 lines. Otherwise, use
        # the full SeqIO.read
        line_limit = 100
        regex_dict = {
            k: re.compile(v) for k, v in six.iteritems({
                'ncbi_accession': r'VERSION\s+([\w.-]+)[^\w.-]',
                'ncbi_assembly': r'Assembly:\s*([\w.-]+)[^\w.-]',
                'ncbi_bioproject': r'BioProject:\s*([\w.-]+)[^\w.-]'
            })
        }
        with open(genbank_filepath, 'r') as f:
            for i, line in enumerate(f.readlines()):
                for key, regex in six.iteritems(regex_dict):
                    match = regex.search(line)
                    if match is not None:
                        out[key] = match.group(1)
                if i > line_limit:
                    break
    else:
        # load the genbank file
        gb_file = _load_gb_file(genbank_filepath)
        out['ncbi_accession'] = gb_file.id
        for value in gb_file.dbxrefs[0].split():
            if 'Assembly' in value:
                out['ncbi_assembly'] = value.split(':')[1]
            if 'BioProject' in value:
                out['ncbi_bioproject'] = value.split(':')[1]

    return out


def load_gene_synonym(session, gene_db, synonym, data_source_id):
    """Load the synonym for this gene from the given genome."""
    data_source_id = get_or_create_data_source(session, data_source_id)
    synonym_db, _ = get_or_create(session, Synonym,
                                  type='gene',
                                  ome_id=gene_db.id,
                                  synonym=synonym,
                                  data_source_id=data_source_id)
    return synonym_db.id


def _get_qual(feat, name, get_first=False):
    """Get a non-null attribute from the feature."""
    try:
        qual = feat.qualifiers[name]
    except KeyError:
        if get_first:
            return None
        else:
            return []

    def nonempty_str(s):
        s = s.strip()
        return None if s == '' else s

    if get_first:
        return nonempty_str(qual[0])
    else:
        return [y for y in (nonempty_str(x) for x in qual)
                if y is not None]


@timing
def load_genome(genome_ref, genome_file_paths, session):
    """Load the genome and chromosomes."""

    if len(genome_file_paths) == 0:
        raise Exception('No files found for genome {}'.format(genome_ref))

    # check that the genome doesn't already exist
    if (session.query(Genome)
        .filter(Genome.accession_type == genome_ref[0])
        .filter(Genome.accession_value == genome_ref[1])).count() > 0:
        raise AlreadyLoadedError('Genome with %s %s already loaded' % genome_ref)

    logging.debug('Adding new genome: {}'.format(genome_ref))
    genome_db = base.Genome(accession_type=genome_ref[0],
                            accession_value=genome_ref[1])
    session.add(genome_db)
    session.commit()

    n = len(genome_file_paths)
    for i, genbank_file_path in enumerate(genome_file_paths):
        logging.info('Loading chromosome [{} of {}] {}'
                     .format(i + 1, n, basename(genbank_file_path)))
        gb_file = _load_gb_file(genbank_file_path)
        load_chromosome(gb_file, genome_db, session)


def load_chromosome(gb_file, genome_db, session):
    chromosome = (session
                  .query(base.Chromosome)
                  .filter(base.Chromosome.ncbi_accession == gb_file.id)
                  .filter(base.Chromosome.genome_id == genome_db.id)
                  .first())
    if not chromosome:
        logging.debug('Loading new chromosome: {}'.format(gb_file.id))
        chromosome = base.Chromosome(ncbi_accession=gb_file.id,
                                     genome_id=genome_db.id)
        session.add(chromosome)
        session.commit()
    else:
        logging.debug('Chromosome already loaded: %s' % gb_file.id)

    # update genome
    if genome_db.organism is None:
        genome_db.organism = gb_file.annotations['organism']

    cobra_id_warnings = 0
    duplicate_genes_warnings = 0
    warning_num = 5
    for i, feature in enumerate(gb_file.features):

        # update genome with the source information
        if genome_db.taxon_id is None and feature.type == 'source':
                for ref in _get_qual(feature, 'db_xref'):
                    if 'taxon' == ref.split(':')[0]:
                        genome_db.taxon_id = ref.split(':')[1]
                        break
                continue

        # only read in CDSs
        if feature.type != 'CDS':
            continue

        # cobra_id required
        cobra_id = None
        gene_name = None
        refseq_name = None
        locus_tag = None

        t = _get_qual(feature, 'locus_tag', True)
        if t is not None:
            locus_tag = t
            cobra_id = scrub_gene_id(t)

        t = _get_qual(feature, 'gene', True)
        if t is not None:
            gene_name = t
            refseq_name = t

        if gene_name is not None and cobra_id is None:
            if cobra_id_warnings <= warning_num:
                msg = 'No locus_tag for gene. Using Gene name as cobra_id: %s' % gene_name
                if cobra_id_warnings == warning_num:
                    msg += ' (Warnings limited to %d)' % warning_num
                logging.warn(msg)
                cobra_id_warnings += 1
            cobra_id = scrub_gene_id(gene_name)
            gene_name = cobra_id
        elif cobra_id is None:
            logging.error(('No locus_tag or gene name for gene %d in chromosome '
                           '%s' % (i, chromosome.ncbi_accession)))
            continue

        gene_db = (session
                   .query(Gene)
                   .filter(Gene.cobra_id == cobra_id)
                   .filter(Gene.chromosome_id == chromosome.id)
                   .first())
        if gene_db is None:
            # get the strand and positions
            strand = None
            if feature.strand == 1:
                strand = '+'
            elif feature.strand == -1:
                strand = '-'
            leftpos = int(feature.location.start)
            rightpos = int(feature.location.end)

            # finally, create the gene
            gene_db = Gene(cobra_id=cobra_id,
                           locus_tag=locus_tag,
                           chromosome_id=chromosome.id,
                           name=gene_name,
                           leftpos=leftpos,
                           rightpos=rightpos,
                           strand=strand,
                           mapped_to_genbank=True)
            session.add(gene_db)
            session.commit()
        else:
            # warn about duplicate genes.
            #
            # TODO The only downside to loading CDS's this way is that the
            # leftpos and rightpos correspond to a CDS, not the whole gene. So
            # these need to be fixed eventually.
            if duplicate_genes_warnings <= warning_num:
                msg = 'Duplicate genes %s on chromosome %s' % (cobra_id, chromosome.id)
                if duplicate_genes_warnings == warning_num:
                    msg += ' (Warnings limited to %d)' % warning_num
                logging.warn(msg)
                duplicate_genes_warnings += 1

        # load the synonyms for the gene
        if locus_tag is not None:
            load_gene_synonym(session, gene_db, locus_tag, 'refseq_locus_tag')

        if refseq_name is not None:
            load_gene_synonym(session, gene_db, refseq_name, 'refseq_name')

        for ref in _get_qual(feature, 'gene_synonym'):
            synonyms = [x.strip() for x in ref.split(';')]
            for syn in synonyms:
                load_gene_synonym(session, gene_db, syn, 'refseq_synonym')

        for ref in _get_qual(feature, 'db_xref'):
            splitrefs = [x.strip() for x in ref.split(':')]
            if len(splitrefs) == 2:
                load_gene_synonym(session, gene_db, splitrefs[1], splitrefs[0])

        for ref in _get_qual(feature, 'old_locus_tag'):
            for syn in [x.strip() for x in ref.split(';')]:
                load_gene_synonym(session, gene_db, syn, 'refseq_old_locus_tag')

        for ref in _get_qual(feature, 'note'):
            for value in [x.strip() for x in ref.split(';')]:
                sp = value.split(':')
                if len(sp) == 2 and sp[0] == 'ORF_ID':
                    load_gene_synonym(session, gene_db, sp[1], 'refseq_orf_id')

    session.commit()
