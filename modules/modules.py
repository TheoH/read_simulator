"""
A module defines a unit of work ( creating a truth VCF/ fasta etc )
"""

import os
import sys
import glob
import logging
import subprocess
from modules_base import ModuleBase
from lib.common import MPdb
from lib.common import PipelineExc, run_process
from lib.common import trim_fasta, trim_vcf, trim_bam
from lib.common import remove_contig_name_descriptions
from lib.common import sort_target_region_bed
from lib.common import create_gold_bam_for_pirs
from lib import bamsurgeon_functions
from lib import bcftools_functions
from lib import vlrd_create_vcf
from lib import rsvsim_create_vcf
from lib import resample_rsvsim_dups_repeats
from enum import Enum
from vlrd import vlrd_functions
import random

import gzip, copy
import shutil
from collections import Counter

this_dir_path = os.path.dirname(os.path.realpath(__file__))

###########################################################
class PrepCNVprobes(ModuleBase):
    default_settings = {}
    expected_settings = ["target_chrs"]
    promised_outputs = ["target_region_bed", "exome_probes"]
    update_db_keys = ["target_region_bed", "cnv_target_bed"]

    def run(self):
        probes = ["/mnt/archive/sim_data/ref_data/capsim/results.txt"]
        probes = probes[0]
        self.logger.info("selected probe file: {}".format(probes))

        # add target chrs info
        target_chrs = self.module_settings["target_chrs"]
        assert not isinstance(target_chrs, list)
        target_chrs = str(target_chrs)
        target_chrs = "chr{}".format(target_chrs.replace("chr", ""))
        if not target_chrs in ["chr{}".format(str(i)) for i in range(1,22)]:
            print "Target chr: {}, not valid selection".format(target_chrs)
        self.module_settings["target_chrs"] = target_chrs

        # new out files
        target_region_bed = os.path.join(self.module_settings["outdir"],
                                         "target_region_bed.txt")

        exome_probes = os.path.join(self.module_settings["outdir"],
                                    "exome_probes.txt")

        try: 
            stream_in = open(probes, 'r') 
            stream_bed_out = open(target_region_bed, 'w')
            stream_probes_out = open(exome_probes, 'w')
            
            line_counter = 0
            target_header = False

            for line in stream_in:
                if target_header:
                    stream_probes_out.write(line)
                    target_header = False
                elif self.module_settings["target_chrs"] in line.split()[0]:
                    target_header = True
                    # probes file
                    stream_probes_out.write(line)
                    # bed
                    _from, _to = line.split(":")[1].split("-")
                    stream_bed_out.write("{}\t{}\t{}".format(
                            self.module_settings["target_chrs"],
                            _from, _to))
                    line_counter += 1
                                         
        except Exception as e:
            raise PipelineExc("Failed to create filtered probe files: {}"
                              "".format(e))
        finally:
            stream_in.close()
            stream_bed_out.close()
            stream_probes_out.close()

        assert line_counter > 5, 'probes do not match chromosome selection'
        self.module_settings["target_region_bed"] = target_region_bed
        self.module_settings["cnv_target_bed"] = target_region_bed
        self.module_settings["exome_probes"] = exome_probes
     


###########################################################
class CNV_Base(ModuleBase):
    default_settings = {}
    expected_settings = [
        "nHomozygousDeletions",
        "nHeterozygousDeletions",
        "nTandemDuplications", 
        "outdir", "workdir", 
        "target_chrs"]
    promised_outputs = [
        'fasta_file',
        'cnv_truth',
        'truth_set_vcf']


    def get_refs(self):
        self.get_dataset_ref()


    def add_contig_names_to_target_bed(self):
        self.module_settings['target_bed_contigs_named'] = "{}_contigs_named".format(
            self.module_settings['target_bed'])
        self.lock.acquire()
        with open(self.module_settings['target_bed']) as stream_in, \
             open(self.module_settings['target_bed_contigs_named'], 'w') as stream_out:
            stream_out.write("contig\tstart\tstop\tname\n")
            for idx, line in enumerate(stream_in):
                new_line = "{}\ttarget-{}\n".format(line.replace("\n",""), idx+1)
                stream_out.write(new_line)
        self.logger.info("Wrote results to: {}".format(
                self.module_settings['target_bed_contigs_named']))
        self.lock.release()
        

    def create_and_submit_truth_tsv_and_vcf(self, csv_files):
        """ create truth files """

        self.logger.info("RSVSim create truth files")
        csv_files = [
            os.path.join(self.module_settings["outdir"], csv) for csv in csv_files]
        resampled_csv_files = []
        
        for f in csv_files:
            assert os.path.isfile(f), "file: {} does not exist".format(f)

            if "tandemDuplications" in f:
                # update the distrubution for the duplications
                self.logger.info("Update TANDUP nr repesats with own distribution")
                dups_file = os.path.join(self.module_settings["outdir"], 
                                         "tandemDuplications.csv")
                dups_file_resampled = os.path.join(self.module_settings["outdir"], 
                                                   "tandemDuplications_resampled.csv")
                resample_rsvsim_dups_repeats.resample_cnv_dup_repeats(
                    dups_file, dups_file_resampled)

                resampled_csv_files.append(dups_file_resampled)
            else:
                resampled_csv_files.append(f)

        truth_vcf, truth_tsv = rsvsim_create_vcf.create_truth_files(
            self.module_settings["fasta_file"],
            self.module_settings['outdir'], resampled_csv_files, self.logger)

        # update db
        self.module_settings['cnv_truth'] = truth_tsv
        self.module_settings['truth_set_vcf'] = truth_vcf

        self.db_api.upload_to_db('cnv_truth', truth_tsv)
        self.db_api.upload_to_db('truth_set_vcf', truth_vcf)


###########################################################
class VCF2BamsurgeonBed(ModuleBase):
    default_settings = {}
    expected_settings = ["truth_set_vcf"]
    promised_outputs = ["bamsurgeon_bed", "truth_set_vcf"]
    update_db_keys = ["truth_set_vcf"]


    def run(self):

        # constants
        new_vcf_path = os.path.join(self.module_settings["outdir"], "truth.vcf")
        new_bed_path = os.path.join(
            self.module_settings["outdir"], "bamsurgeon_truth.bed")

        # execute
        bamsurgeon_functions.vcf2bed(
            self.module_settings["truth_set_vcf"], 
            new_vcf_path, 
            new_bed_path, 
            self.logger)

        # update module settings
        self.module_settings["truth_set_vcf"] = new_vcf_path
        self.module_settings["bamsurgeon_bed"] = new_bed_path


###########################################################
class Bamsurgeon(ModuleBase):
    default_settings = {}
    expected_settings = ["pon_vcf", "cosmic_vcf", "dbsnp_vcf",
                         "bamsurgeon_bed", "dragen_BAM"]
    promised_outputs = ["dragen_BAM"]
    update_db_keys = ["dragen_BAM", "pon_vcf", "cosmic_vcf", "dbsnp_vcf"]

    def run(self):
        self.get_dataset_ref()        
        script = os.path.join(this_dir_path, "..",
                              "bin", "run_bamsurgeon.sh")
        cmd = script
        cmd += " -f {fasta_file} -w {workdir} -o {outdir} -b tumor.bam "
        cmd += " -n {dragen_BAM} -e {bamsurgeon_bed}"
        cmd = cmd.format(**self.module_settings)

        # run bam surgeon
        run_process(cmd, self.logger)

        # update module settings
        self.module_settings["dragen_BAM"] = os.path.join(
            self.module_settings['outdir'], "tumor.bam")
        assert os.path.isfile(self.module_settings["dragen_BAM"])


###########################################################
class ChrTrimmer(ModuleBase):
    default_settings = {}
    expected_settings = ["truth_set_vcf", 
                         "target_chrs", "shared_dir"]
    promised_outputs = ["fasta_file", "truth_set_vcf", "hash_table6"]
    update_db_keys = ["truth_set_vcf"]

    def run(self):
        self.get_dataset_ref()

        # make sure we have a clean fasta
        remove_contig_name_descriptions(self)

        if not self.module_settings.get("target_chrs", None):
            self.logger.info("Use full FASTA and VCF for simulation")
            return 

        # if we should only use a subset (e.g. chr20) 
        # of the input fasta/ vcf for speed
        self.logger.info("Trim FASTA and VCF to small chromosome")
        trim_fasta(self)
        trim_vcf(self)

        # BAM is optional
        if self.module_settings.get("dragen_BAM", None):
            self.logger.info("TRIM BAM")
            self.promised_outputs.append("dragen_BAM")
            self.update_db_keys.append("dragen_BAM")
            trim_bam(self)

###########################################################
class FastaTrimmer(ModuleBase):
    default_settings = {}
    expected_settings = ["target_chrs", "shared_dir"]
    promised_outputs = ["fasta_file", "hash_table6"]

    def run(self):
        self.get_dataset_ref()
        assert self.module_settings['hash_table6']

        # make sure we have a clean fasta
        remove_contig_name_descriptions(self)

        target_chrs = self.module_settings.get("target_chrs", None)
        if ( not target_chrs ) or ( target_chrs == "ALL" ):
            self.logger.info("Use full FASTA for simulation")
        else:
            # if we should only use a subset (e.g. chr20) 
            self.logger.info("Trim FASTA")
            trim_fasta(self)


###########################################################
class BedTrimmer(ModuleBase):
    default_settings = {}
    expected_settings = ["target_chrs", "target_region_bed"]
    promised_outputs = ["target_region_bed"]
    update_db_keys = ["target_region_bed"]

    def run(self):
        # should we only use a subset (e.g. chr20)?
        target_chrs = self.module_settings.get("target_chrs", None)
        if target_chrs:
            target_chrs = str(target_chrs)

        if (not target_chrs) or (target_chrs.lower() == "all"):
            self.logger.info("Use full bed for simulation")
            target_bed = self.module_settings['target_region_bed']
            self.module_settings["target_region_bed"] = target_bed
            self.module_settings["cnv_target_bed"] = target_bed
            self.db_api.upload_to_db("cnv_target_bed", target_bed)
            self.db_api.upload_to_db("truth_set_bed", target_bed)
            return 

        self.logger.info("Trim BED to small chromosome")
        array_lines = []
        array_regions = []
        vlrd_mode = False

        with open(self.module_settings["target_region_bed"]) as stream:          
            for line in stream:
                # target_chrs can be an array or single string
                if type(target_chrs) in [unicode, str]:
                    if line.split()[0] != target_chrs:
                        continue
                else:
                    if line.split()[0] not in target_chrs:
                        continue
                
                # lines to keep
                array_lines.append(line)
                if len(line.split()) == 5:
                    array_regions.append(line.split()[3])
                    vlrd_mode = True

        # is this a vlrd bed? then we should only keep paired regions
        if vlrd_mode:
            hist = Counter(array_regions) # e.g. {"2": 3, "54": 3}
            regions_to_keep = []
            for key in hist:
                if hist[key] == 2:
                    regions_to_keep.append(key)
            regions_to_keep = set(regions_to_keep)

            for i in reversed(range(0,len(array_lines))):
                if array_lines[i].split()[3] not in regions_to_keep:
                    array_lines.pop(i)

        if not array_lines:
            raise PipelineExc("Empty target bed")

        # write results
        new_target_bed = os.path.join(
            self.module_settings['outdir'],
            'trimmed_target_bed.txt')

        with open(new_target_bed, 'w') as stream:
            for line in array_lines:
                stream.write(line)
                              
        # update results
        self.module_settings["target_region_bed"] = new_target_bed
        self.module_settings["cnv_target_bed"] = new_target_bed
        self.db_api.upload_to_db("cnv_target_bed", new_target_bed)
        self.db_api.upload_to_db("truth_set_bed", new_target_bed)


###########################################################
class VCF2Fastas(ModuleBase):
    default_settings = {}
    expected_settings = ["outdir", "workdir", "truth_set_vcf",
                         "fasta_file"]
    promised_outputs = ["mod_fasta_1", "mod_fasta_2", "liftoverBasename"]
    
    def create_fastas(self):
        """
        use bcftools to create 2 modified fastas
        that can be used for read simulation
        """
            
        # only modify the fastas if there are variants in the VCFs
        found_a_variant = 0
        zipped = ".gz" in self.module_settings["truth_set_vcf"]
        open_safe = gzip.open if zipped else open
        with open_safe(self.module_settings["truth_set_vcf"]) as stream_in:
            for line in stream_in:
                if line[0] == "#":
                    continue
                found_a_variant = 1
                break

        # use the unmodified fasta to simulate reads if there are no variants
        if not found_a_variant:
            self.logger.info("No variants added to Fastas")
            self.module_settings["mod_fasta_1"] = self.module_settings["fasta_file"]
            self.module_settings["mod_fasta_2"] = self.module_settings["fasta_file"]
            self.module_settings["liftoverBasename"] = "NONE"
        else:
            # else create two modified fastas
            cmd_template = "bcftools consensus -c {liftover} -H {haplotype} " + \
                "-f {fasta_file} {truth_set_vcf}"

            liftoverBasename = os.path.join(
                self.module_settings["outdir"], "dsim")
            self.module_settings["liftoverBasename"] = liftoverBasename

            for hap in [1, 2]:
                mod_fasta = os.path.join(
                    self.module_settings["outdir"], "mod_fasta_{}.fa".format(hap))
                liftover = "{}_{}{}".format(
                    liftoverBasename, hap, ".liftover.txt")
                options = {
                    "liftover": liftover, 
                    "haplotype": hap, 
                    "fasta_file": self.module_settings["fasta_file"],
                    "truth_set_vcf": self.module_settings['truth_set_vcf']}
                cmd = cmd_template.format(**options)
                try:
                    run_process(cmd, self.logger, mod_fasta)
                except:
                    self.logger.info("BCFTools failed. Let's continue and hope for the best")

                # update pipeline settings
                self.module_settings["mod_fasta_{}".format(hap)] = mod_fasta
                print "create fasta {}".format(hap)


    def run(self):
        self.create_fastas()
        

###########################################################
class RSVSIM_VCF(CNV_Base):
    expected_settings = [
        "size_ins", "size_dels", "size_dups", 
        "nr_ins", "nr_dels", "nr_dups",
        "outdir", "workdir", "target_chrs", "shared_dir", "cnv_target_bed"]
    update_db_keys = ["cnv_target_bed", "truth_set_vcf"]
    promised_outputs = ["truth_set_vcf", "fasta_file"]

    def copy_workdir_to_outdir(self):
        src = self.module_settings["workdir"]
        dest = self.module_settings["outdir"]
        src_files = os.listdir(src)
        for file_name in src_files:
            full_file_name = os.path.join(src, file_name)
            if (os.path.isfile(full_file_name)):
                shutil.copy(full_file_name, dest)

            
    def create_truth_vcf(self):
        # Rscript $cnvsimultoolsdir/RSVSim_generate_cnv.R \
        #     $chrom $gencnvdir $sizeins $sizedel $sizedup
        _rsv_script = os.path.join(this_dir_path, "cnv_whg", "RSVSim_generate_cnv.R")
        cmd = _rsv_script
        cmd += " --outdir {workdir}"
        cmd += " --size_ins {size_ins}"
        cmd += " --size_del {size_dels}"
        cmd += " --size_dup {size_dups}"
        cmd += " --nr_ins {nr_ins}"
        cmd += " --nr_dels {nr_dels}"
        cmd += " --nr_dups {nr_dups}"
        cmd += " --target_chrs {target_chrs}"
        cmd = cmd.format(**self.module_settings)
        run_process(cmd, self.logger)

        # copy to NAS
        self.copy_workdir_to_outdir()

        # truth files (files are in outdir )
        csv_files = ["deletions.csv", "insertions.csv",
                     "tandemDuplications_resampled.csv"]
        self.create_and_submit_truth_tsv_and_vcf(csv_files)

    def run(self):
        self.get_dataset_ref()
        self.create_truth_vcf()
  

###########################################################
class CNVgdbVCF(CNV_Base):
    default_settings = {}
    expected_settings = [
        "nr_dels",
        "nr_dups",
        "nr_ins",
        "outdir",
        "target_chrs",
        "target_region_bed"]
    update_db_keys = ["cnv_target_bed"]
    promised_outputs = ['cnv_truth', 'truth_set_vcf', "cnv_target_bed"]

    def run(self):
        self.module_settings["DGV"] = "/mnt/archive/sim_data" + \
            "/ref_data/cnv/GRCh37_hg19_variants_2016-05-15.txt"

        # update cnv target bed
        self.module_settings["cnv_target_bed"] = \
            self.module_settings["target_region_bed"]

        _mod_fastas_script = os.path.join(
            this_dir_path, "..", "bin", "cnv_exome_variant_simulator.R")

        _log = os.path.join(self.module_settings['outdir'], "rsvsim.log")

        cmd = "/usr/bin/Rscript "
        cmd += _mod_fastas_script
        cmd += " --nrDeletions {nr_dels}"
        cmd += " --nrDuplications {nr_dups}"
        cmd += " --outdir {outdir}"
        cmd += " --fasta {fasta_file}"
        cmd += " --target_chrs {target_chrs}"
        cmd += " --target_bed {target_region_bed}"
        cmd += " --DGV {DGV}"
        cmd = cmd.format(**self.module_settings)
        run_process(cmd, self.logger, outfile=None, 
                    _cwd=self.module_settings['outdir'])
        
        self.logger.info("RSVSim completed")

        # create truth
        csv_files = ["deletions.csv", "tandemDuplications.csv"]
        self.create_and_submit_truth_tsv_and_vcf(csv_files)


###########################################################
class Capsim(ModuleBase):
    default_settings = {}

    expected_settings = ["number-of-reads", "read-len", "fragment-size", 
                         "mod_fasta_1", "mod_fasta_2"]

    promised_outputs =  ["fastq_location_1", "fastq_location_2",
                         "fastq_offser_override", "fastq_offser_override_detail"]

    update_db_keys = ["fastq_location_1", "fastq_location_2",
                      "fastq_offser_override", "fastq_offser_override_detail",
                      "gold_roc_flag"]

    def run(self):
        # run
        self.logger.info('Capsim: simulating reads ...')
        script_path = os.path.join(this_dir_path, "../bin", "run_capsim.sh")
        log = os.path.join(self.module_settings['outdir'], "capsim.log")
        self.module_settings['capsim_log'] = log
           
        # build cmd
        cmd = script_path + \
            " -n {number-of-reads}" + \
            " -l {read-len}" + \
            " -f {fragment-size}" + \
            " -o {outdir}" + \
            " -1 {mod_fasta_1} -2 {mod_fasta_2}" + \
            " -p {exome_probes} "
        cmd = cmd.format(**self.module_settings)
        
        run_process(cmd, self.logger)

        # update db with results
        self.logger.info('find the Capsim generated FQs and upload to DB')
        fq_lists = []
        for i in ["1", "2"]:
            p = os.path.join(self.module_settings["outdir"], 
                             'output_%s.fastq.gz' % i)
            fq_lists.append(glob.glob(p))

        self.module_settings["fastq_location_1"] = fq_lists[0][0]
        self.module_settings["fastq_location_2"] = fq_lists[1][0]
        self.module_settings["fastq_offser_override"] = 1
        self.module_settings["gold_roc_flag"] = 1
        self.module_settings["fastq_offser_override_detail"] = 33


###########################################################
class VLRD_VCF_FASTA(ModuleBase):
    default_settings = {}
    expected_settings = ["varrate"]
    promised_outputs = ["mod_fasta_1", "mod_fasta_2", "fasta_file",
                        "ref_type", "truth_set_vcf"]
    # only update the db with the provided bed 
    # but use the sorted bed for simulations 
    update_db_keys = ["target_region_bed", "truth_set_vcf"]

    def run(self):
        self.get_dataset_ref()
        target_region_bed = self.module_settings.get(
            "target_region_bed", None)

        if target_region_bed:
            sort_target_region_bed(self)

        # the user may provide the truth vcf
        vcf = self.module_settings.get("truth_set_vcf", None)
        if os.path.isfile(vcf):
            self.logger.info("Truth VCF provided")
        else:
            self.logger.info("Create truth VCF")
            vlrd_functions.create_truth_vcf_and_fastas(
                self.module_settings)


###########################################################
class VLRD_VCF(ModuleBase):
    default_settings = {}
    expected_settings = ["varrate", "target_region_bed"]
    promised_outputs = ["truth_set_vcf"]
    update_db_keys = ["target_region_bed", "truth_set_vcf"]

    def run(self):
        sort_target_region_bed(self)
        # the module setings will be updated
        vcf = self.module_settings.get("truth_set_vcf", None)
        if os.path.isfile(vcf):
            self.logger.info("Truth VCF provided")
        else:
            self.logger.info("Create truth VCF")
            vlrd_create_vcf.create_truth_vcf(
                self.module_settings)


###########################################################
class ContigComboType(Enum):
    alt_prim = 1
    same_alts = 2
    diff_alts = 3


class AltContigVCF(ModuleBase):
    default_settings = {}
    expected_settings = ["contig-1", "contig-2", "alt-sam"]
    promised_outputs = ["mod_fasta_1", "mod_fasta_2"]


    def check_output(self, cmd):
        """ helper function to run cmd """
        self.logger.info("Run cmd: {}".format(cmd))
        res = subprocess.check_output(cmd, shell=True)
        self.logger.info("Response: {}".format(res))
        return res

    def identify_contig_types_and_use_case(self):
        """
        for the 2 contigs; the user can specify any of the following 3 contig combo cases
        1. primary + alt_contig ( we also want to standardize this order )
        2. alt_contig1 + alt_contig1
        3. alt_contig1 + alt_contig2(any 2 different, but related, contigs)
        """

        # identify type for each contig
        contig_is_primary = {}
        for contig in ["contig-1", "contig-2"]:
            contig_is_primary[contig] = True
            for sufx in ['_alt', 'HLA']:  # these markers help identify alt contigs
                if sufx in self.module_settings[contig]:
                    contig_is_primary[contig] = False

        # both contigs should not be primary
        if all(contig_is_primary.values()):  # if all([contig_is_primary[key] for key in ["contig-1", "contig-2"]):
            raise PipelineExc("Use case not supported where both contigs are primary")

        # one contig is an alt and the other is a primary
        elif contig_is_primary["contig-1"] != contig_is_primary["contig-2"]:
            self.module_settings["contigs_combo_type"] = ContigComboType.alt_prim

            # standardize with contig-2 being the primary
            # contig 1 will now always be an alt
            if contig_is_primary["contig-1"]:
                _tmp = self.module_settings["contig-1"]
                self.module_settings["contig-1"] = self.module_settings["contig-2"]
                self.module_settings["contig-2"] = _tmp

        # both contigs are alt-contigs
        else:
            # are they the same alt?
            if self.module_settings["contig-1"] == self.module_settings["contig-2"]:
                self.module_settings["contigs_combo_type"] = ContigComboType.same_alts
            else:
                self.module_settings["contigs_combo_type"] = ContigComboType.diff_alts

    def get_contig_length_from_cfg(self, contig_key):
        """ lookup alt contig length in dict """
        assert contig_key in ["contig-1", "contig-2"]
        logger.info("Get contig {} ranges".format(contig_key))

        try:
            c_idx = contig_key
            c_idx_from = c_idx + "-from"
            c_idx_to = c_idx + "-to"
            contig_name = self.module_settings[c_idx]
            self.module_settings[c_idx_from] = 1
            self.module_settings[c_idx_to] = self.module_settings["ht_table_config"][contig_name]
        except Exception as e:
            raise PipelineExc("Contig not found: {}".format(e))

        for i in [c_idx_from, c_idx_to]:
            assert isinstance(self.module_settings[i], int), \
                "Invalid contig ranges detected"

    def get_contig_start_stop_indexes(self):
        """ a specific range will be available on the primary, alt 1 and alt 2 
        if assume the whole of alt 1 will be represented
        now find corresponding region in primary """

        # get contig 1 start stop indexes
        self.get_contig_length_from_cfg("contig-1")

        # get primary contig start stop indexes
        self.logger.info("Find primary contig start stop indexes")
        _alt_to_pri_script = os.path.join(this_dir_path, "alt_contig", "convert_alt_to_pri_coords.pl")
        cmd = "{} {} {} 1 {} 2> /dev/null".format(
            _alt_to_pri_script,
            self.module_settings["alt-sam"],
            self.module_settings["contig-1"],
            self.module_settings["contig-1-to"])

        cmd = cmd.format(**self.module_settings)
        res = self.check_output(cmd)

        try:
            _from, _to = res.split()[1:3]
            self.module_settings["contig-primary-from"] = _from
            self.module_settings["contig-primary-to"] = _to
        except Exception as e:
            raise PipelineExc('Failed to find start-stop index in primary {}'.format(e))

        # get contig 2 start stop indexes ( context specific )
        self.logger.info("Find indexes in alt-contig 2 that corresponds to subrange in primary ")
        if self.module_settings["contigs_combo_type"] == ContigComboType.alt_prim:
            self.module_settings["contig-2-from"] = self.module_settings["contig-primary-from"]
            self.module_settings["contig-2-to"] = self.module_settings["contig-primary-to"]
        elif self.module_settings["contigs_combo_type"] == ContigComboType.same_alts:
            self.module_settings["contig-2-from"] = self.module_settings["contig-1-from"] 
            self.module_settings["contig-2-to"] = self.module_settings["contig-1-to"] 
        elif self.module_settings["contigs_combo_type"] == ContigComboType.diff_alts:
            # find range in contig 2 ( alt contig ) that maps to alt contig 1
            _pri_to_alt_script = os.path.join(this_dir_path, "alt_contig", "convert_pri_to_alt_coords.pl")
            cmd = "{} {} {} {} {} 2> /dev/null".format(
                _pri_to_alt_script,
                self.module_settings["alt-sam"],
                self.module_settings["contig-2"],
                self.module_settings["contig-primary-from"],
                self.module_settings["contig-primary-to"])
            res = self.check_output(cmd)
            try:
                _from, _to = res.split()[4:6]
                self.module_settings["contig-2-from"] = _from
                self.module_settings["contig-2-to"] = _to
            except Exception as e:
                raise PipelineExc('Failed to find start-stop indexes in alt-contig-2 {}'.format(e))

    def create_modified_fastas(self):
        """ create fastas for read simulator """

        def check_call():
            try:
                self.logger.info(cmd)
                subprocess.check_call(cmd, shell=True)
            except Exception as e:
                raise PipelineExc(e)

        fastas = {"contig-1": "{outdir}/mod_fasta_1.fa".format(**self.module_settings),
                  "contig-2": "{outdir}/mod_fasta_2.fa".format(**self.module_settings)}

        for c in ["contig-1", "contig-2"]:
            cmd = "samtools faidx {} {}:{}-{} > {}"
            cmd = cmd.format(self.module_settings["fasta_file"],
                             self.module_settings[c],
                             self.module_settings[c + "-from"],
                             self.module_settings[c + "-to"],
                             fastas[c])
            check_call()

        self.module_settings["mod_fasta_1"] = fastas["contig-1"]
        self.module_settings["mod_fasta_2"] = fastas["contig-2"]


    def create_truth_vcf(self):
        script_path = os.path.join(this_dir_path, "alt_contig", "fasta_sam_to_vcf.pl")
        self.module_settings["truth_vcf"] = os.path.join(self.module_settings["outdir"], "alt_contig_truth.vcf")
        cmd = script_path + " {fasta_file} {alt-sam} {contig-1}:{contig-1-from}-{contig-1-to} " + \
            "{contig-2}:{contig-2-from}-{contig-2-to} > {truth_vcf}"
        cmd = cmd.format(**self.module_settings)
        try: 
            self.check_output(cmd)
        except Exception as e:
            raise PipelineExc("Failed to create truth VCF: {}".format(e))
        self.db_api.post_truth_vcf(self.module_settings["truth_vcf"])

    def run(self):
        self.get_dataset_ref()
        self.get_ht_cfg()
        self.identify_contig_types_and_use_case()
        self.get_contig_start_stop_indexes()
        self.create_modified_fastas()
        self.create_truth_vcf()


###########################################################
class Pirs(ModuleBase):
    default_settings = {"use_mason_profiles": False}

    expected_settings = ["mod_fasta_1", "mod_fasta_2", "gc_bias",
                         "fasta_file", "coverage", "read_len", "use_mason_profiles"]
    promised_outputs = ["fastq_location_1", "fastq_location_2", "read_info"]
    update_db_keys = ["fastq_location_1", "fastq_location_2", "gold_roc_flag"]


    def set_profiles(self):
        if self.module_settings.get('use_mason_profiles'):
            if int(self.module_settings['read_len']) > 400:
                raise PipelineExc("read_len must be <= {} bp".format(400))
            self.module_settings["snp_err"] = os.path.join(
                this_dir_path, '..',
                'pirs_profiles',
                'mason_400.count.matrix')
            self.module_settings["indel_err"] = os.path.join(
                this_dir_path, '..', 
                'pirs_profiles',
                'mason_400.InDel.matrix')
            self.module_settings["gcdep"] = ""
        elif self.module_settings.get('gen_new_mason_profiles'):
            # build mason profiles
            if not self.module_settings.get('hash_table6'):
                print "need a hash table"
                import pdb; pdb.set_trace()
                sys.exit(1)

            # RUN MASON       
            self.logger.info('Run Mason')
 
            rl = self.module_settings['read_len']
            out_path = os.path.join(self.module_settings['outdir'], 
                                    'mason_{}.fastq'.format(rl))

            import random
            seed = random.randint(7000,8000)
            
            opt = {'read_len': rl,
                   'out_path': out_path,
                   'fasta_file': self.module_settings['fasta_file'], 
                   'seed': seed}
        
            cmd="/opt/mason-0.1.2/mason illumina -sq -hn 2 -hs 0 -hi 0 -pi 0.001 "
            cmd+=" -pd 0.001 -pmm 0.004  -s {seed} "
            cmd+=" -N 500000 -n {read_len} -ll 600 -le 60 "
            cmd+=" -o {out_path}"
            cmd+=" -mp {fasta_file} -rnp TEMP"
            cmd = cmd.format(**opt)

            run_process(cmd, self.logger)
        
            self.logger.info('Find the mason generated FQs')
            fq_list = [os.path.join(self.module_settings["outdir"], 
                                    'mason_{}_{}.fastq'.format(rl, i)) 
                       for i in [1,2]]
            mason_sam = os.path.join(self.module_settings["outdir"], 
                                     'mason_{}.fastq.sam'.format(rl)) 

            # RUN DRAGEN AND GENERATE BAM
            self.logger.info("Run DRAGEN and generate BAM")
            opt = {'fq1': fq_list[0],
                   'fq2': fq_list[1],
                   'outdir': self.module_settings['outdir'],
                   'hash_table6': self.module_settings['hash_table6']}

            cmd = "/opt/edico/bin/dragen  -1 {fq1} -2 {fq2} --enable-map-align-output=true  "
            cmd += "--enable-bam-indexing=true "
            cmd += "--enable-sort=true --enable-map-align=true  --force --ref-dir={hash_table6} "
            cmd += "--output-directory={outdir} --output-file-prefix=dragen_mason"
            cmd = cmd.format(**opt)

            self.lock.acquire()
            self.logger.info("Running DRAGEN ...")
            run_process(cmd, self.logger)
            dragen_bam = os.path.join(self.module_settings['outdir'], 'dragen_mason.bam')
            self.logger.info("DRAGEN complete")
            self.lock.release()

        
            # USE PIRS TO REVERSE ESIMATE THE PROFILES
            path = os.path.join(self.module_settings['outdir'], "mason_{}".format(rl))
            opt = {'path': path,
                   'bam': dragen_bam, 
                   'ref': self.module_settings['fasta_file']}
            
            self.logger.info('calc base quality profile ...')
            cmd = "/opt/pirs-2.0.1/baseCalling_Matrix_calculator "
            cmd += "-i {bam} -r {ref} -o {path}"
            cmd = cmd.format(**opt)
            run_process(cmd, self.logger)
            self.logger.info('done')

            self.logger.info('calc indel noise profile ...')
            cmd = "/opt/pirs-2.0.1/indelstat_sam_bam {bam} {path}"
            cmd = cmd.format(**opt)
            run_process(cmd, self.logger)
            self.logger.info('done')

            # UPDATE PROFILES        
            rl = self.module_settings['read_len']
            path = os.path.join(self.module_settings['outdir'], "mason_{}".format(rl))
            self.module_settings["gcdep"] = ""
            self.module_settings["snp_err"] = path + ".count.matrix"
            self.module_settings["indel_err"] = path + ".InDel.matrix"
        else:
            self.logger.info("Use Pirs provided profile")
            if int(self.module_settings['read_len']) > 100:
                raise PipelineExc("read_len must be <= {} bp".format(100))
            self.module_settings["snp_err"] = \
                "/opt/pirs-2.0.1/Profiles/Base-Calling_Profiles" + \
                "/humNew.PE100.matrix.gz"
            self.module_settings["indel_err"] = \
                "/opt/pirs-2.0.1/Profiles/InDel_Profiles/phixv2.InDel.matrix"
            self.module_settings["gcdep"] = \
                "/opt/pirs-2.0.1/Profiles/GC-depth_Profiles/humNew.gcdep_100.dat"


    def create_reads(self):
        # run pirs and create fqs
        self.module_settings["insert-len-mean"] = 4 * int(self.module_settings["read_len"])
        self.logger.info('Pirs: simulating reads ...')
        pirs_log = os.path.join(self.module_settings['outdir'], "pirs.log")
        # " --no-indel-errors"
        cmd = "pirs simulate -l {read_len} -x {coverage} -o {workdir}/pirs" + \
              " --insert-len-mean={insert-len-mean} --insert-len-sd=40 --diploid " + \
              " --phred-offset=33 -c gzip " + \
              " -t 48 {mod_fasta_1} {mod_fasta_2} " + \
              " --indel-error-profile={indel_err} " + \
              " --base-calling-profile={snp_err} "  

        cmd = cmd.format(**self.module_settings)
        if self.module_settings['gc_bias']:
            cmd += " --gc-bias-profile={}".format(
                self.module_settings['gcdep'])
        else:
            cmd += " --no-gc-bias "

        self.logger.info('writing log to: {}'.format(pirs_log))
        run_process(cmd, self.logger, pirs_log)

        # put FQs in DB
        self.logger.info('Find the pirs generated FQs and upload to DB')
        fq_list = []
        for i in ["1", "2"]:
            p = os.path.join(
                self.module_settings["workdir"],
                'pirs*{}_{}.fq.gz'.format(
                    self.module_settings["insert-len-mean"], i))
            p_res = glob.glob(p)

            if len(p_res) != 1: 
                raise PipelineExc("Too many/few matching fastqs found in "
                                  "Pirs output folder: {}".format(p_res))
            fq_list.append(p_res[0])

        # find on staging
        self.module_settings["fastq_location_1"] = fq_list[0]
        self.module_settings["fastq_location_2"] = fq_list[1]
        # copy to NAS
        self.copy_workdir_to_outdir("fastq_location_1")
        self.copy_workdir_to_outdir("fastq_location_2")

    def validate_fastas(self):
        try:
            for i in ["mod_fasta_1", "mod_fasta_2"]:
                assert os.path.isfile(self.module_settings[i])
        except Exception as e:
            raise PipelineExc('Pirs is missing a required modified Fasta')

    def upload_read_info(self):
        p = os.path.join(self.module_settings["workdir"], 'pirs*read.info.gz')
        l = glob.glob(p)
        if os.path.isfile(l[0]):
            self.module_settings['read_info'] = l[0]
        else:
            raise PipelineExc("Failed to find read_info: {}".format(p))
        self.copy_workdir_to_outdir("read_info")

    def run(self):
        self.set_profiles()
        self.validate_fastas()
        self.create_reads()
        self.upload_read_info()
        self.module_settings["gold_roc_flag"] = 1


###########################################################
class PirsGoldBam(ModuleBase):
    default_settings = {}
    expected_settings = ["outdir", "liftoverBasename", "read_info"]
    promised_outputs = ["sam_gold"]
    update_db_keys = ["sam_gold"]

    def run(self):
        create_gold_bam_for_pirs(self)
        self.copy_workdir_to_outdir("sam_gold")


###########################################################
class Mason(ModuleBase):
    default_settings = {}
    expected_settings = ["fasta_file", "coverage", "read_len"]
    promised_outputs = ["fastq_location_1", "fastq_location_2", "sam_gold"]
    update_db_keys = ["fastq_location_1", "fastq_location_2", "sam_gold"]   

    def run(self):
        # run
        self.logger.info('Mason: simulating reads ...')
        _log = os.path.join(self.module_settings['outdir'], "mason.log")

        _ref_length = 0
        with open(self.module_settings["fasta_file"]) as stream:
            for line in stream:
                _ref_length += len(line)

        _number_reads = _ref_length * int(self.module_settings["coverage"]) / \
            int(self.module_settings["read_len"])
                
        cmd = "/opt/mason-0.1.2/mason illumina -sq -hn 2 -pi 0.001 "
        cmd += " -pd 0.001 -pmm 0.004  -s 7451 "
        cmd += " -N {} -n {} -ll 410 -le 22  ".format(
            _number_reads, self.module_settings["read_len"])
        cmd += " -rnp {}".format(self.module_settings["dataset_name"])
        cmd += " -o {}/{}_pi_0.001_pd_0.001_pmm_0.004_s_7451_N{}_n_{}_ll_410_le_22.fastq" \
        "".format(
            self.module_settings["outdir"], 
            self.module_settings["dataset_name"],
            _number_reads,
            self.module_settings["read_len"])
        cmd += " -mp {} -vcf {}".format(
            self.module_settings["fasta_file"], self.module_settings["truth_set_vcf"])
        cmd += " --include-read-information"

        self.logger.info('writing log to: {}'.format(_log))
        self.logger.info(cmd)
        run_process(cmd, self.logger, _log)

        # put FQs in DB
        self.logger.info('Find the mason generated FQs and upload to DB')
        fq_list = []
        for i in ["1", "2"]:
            p = os.path.join(self.module_settings["outdir"], '*{}.fastq'.format(i))
            p_res = glob.glob(p)

            if len(p_res) != 1: 
                raise PipelineExc("Too many/few matching fastqs found in "
                                  "mason output folder: {}".format(p_res))
            fq_list.append(p_res[0])

        self.logger.info('Find the mason generated sam and upload to DB')                                        
        p = os.path.join(self.module_settings["outdir"], '*fastq.sam')
        p_res = glob.glob(p)
        sam_gold = p_res[0]

        self.module_settings["fastq_location_1"] = fq_list[0]
        self.module_settings["fastq_location_2"] = fq_list[1]
        self.module_settings["sam_gold"] = sam_gold


###########################################################
class Pirs_Tumor(ModuleBase):
    default_settings = {
        "insert-len-mean": 800,
        "PE100": "/opt/pirs-2.0.1/Profiles/Base-Calling_Profiles/humNew.PE100.matrix.gz",
        "indels": "/opt/pirs-2.0.1/Profiles/InDel_Profiles/phixv2.InDel.matrix",
        "gcdep": "/opt/pirs-2.0.1/Profiles/GC-depth_Profiles/humNew.gcdep_100.dat",
    }
    expected_settings = ["PE100", "indels", "gcdep", "mod_fasta_1", "mod_fasta_2",
                         "fasta_file", "tumor_cov", "non_tumor_cov"]

    promised_outputs = []

    def run(self):
        # create reads for tumor sample 
        # non-tumor reads are included in tumor fastq sheet to adjust allele frequencies
        # the actual "normal" fqs for use in tumor-normal runs are simulated seperately
        self.logger.info('Pirs Tumor: simulating tumor reads ...')
        log_t = os.path.join(self.module_settings['outdir'], "pirs_tumor.log")
        log_nt = os.path.join(self.module_settings['outdir'], "pirs_non_tumor.log")

        # need to update the module settings
        for i in ["mod_fasta_1", "mod_fasta_2", "fasta_file"]:
            self.module_settings[i] = self.module_settings[i]

        # tumor
        tumor_cmd = \
            "pirs simulate -l 100 -x {tumor_cov} -o {outdir}/pirs_tumor" + \
            " --insert-len-mean={insert-len-mean} --insert-len-sd=40 --diploid " + \
            " --base-calling-profile={PE100}" + \
            " --indel-error-profile={indels}" + \
            " --gc-bias-profile={gcdep}" 
        stream_out.write("RGPL,RGID,RGSM,RGLB,Lane,Read1File,RGCN,Read2File,RGDS,RGDT,RGPI\n")
        tmp = "DRAGEN_RGPL,DRAGEN_RGID_tumor_{},sim_tumor,ILLUMINA,{}" + \
              ",{},DRAGEN_RGCN,{},DRAGEN_RGDS,DRAGEN_RGDT,DRAGEN_RGPI\n"
        line = tmp.format(1, 1, tumor_fqs[0], tumor_fqs[1])
        stream_out.write(line)

        line = tmp.format(2, 2, non_tumor_fqs[0], non_tumor_fqs[1])
        stream_out.write(line)
        self.db_api.upload_to_db('csv_list', fq_list)


###########################################################
class CleanVCF(ModuleBase):
    default_settings = {}
    expected_settings = ["outdir"]
    promised_outputs = ["truth_set_vcf"]

    def create_truth_vcf(self):
        truth_vcf = os.path.join(self.module_settings['outdir'], 'truth.vcf')
        with open(truth_vcf, 'w') as stream:
            stream.write('##fileformat=VCFv4.2\n')
            stream.write('#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n')
        self.db_api.post_truth_vcf(truth_vcf)
        
        cmd = ["bgzip", "-f", truth_vcf]
        run_process(cmd, self.logger)
        truth_vcf = truth_vcf + ".gz"

        cmd = ["bcftools", "index", truth_vcf]
        run_process(cmd, self.logger)

        self.module_settings["truth_set_vcf"] = truth_vcf

    def run(self):
        self.create_truth_vcf()


###########################################################
class TestMod(ModuleBase):
    """ module for quick functional test """
    default_settings = {}
    expected_settings = ["test"]
    promised_outputs = ["test_out"]

    def run(self):
        self.module_settings["test"] = "test"
        self.module_settings["test_out"] = "test_out"
