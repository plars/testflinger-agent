# Copyright (C) 2017 Canonical
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>

import json
import logging
import multiprocessing
import os
import shutil

from testflinger_agent.job import TestflingerJob
from testflinger_agent.errors import TFServerError

logger = logging.getLogger(__name__)


class TestflingerAgent:
    def __init__(self, client):
        self.client = client

    def get_offline_file(self):
        return os.path.join(
            '/tmp',
            'TESTFLINGER-DEVICE-OFFLINE-{}'.format(
                self.client.config.get('agent_id')))

    def check_offline(self):
        return os.path.exists(self.get_offline_file())

    def check_job_state(self, job_id):
        job_data = self.client.get_result(job_id)
        if job_data:
            return job_data.get('job_state')

    def mark_device_offline(self):
        # Create the offline file, this should work even if it exists
        open(self.get_offline_file(), 'w').close()

    def process_jobs(self):
        """Coordinate checking for new jobs and handling them if they exists"""
        TEST_PHASES = ['setup', 'provision', 'test', 'reserve']

        # First, see if we have any old results that we couldn't send last time
        self.retry_old_results()

        job_data = self.client.check_jobs()
        while job_data:
            job = TestflingerJob(job_data, self.client)
            logger.info("Starting job %s", job.job_id)
            rundir = os.path.join(
                self.client.config.get('execution_basedir'), job.job_id)
            os.makedirs(rundir)
            # Dump the job data to testflinger.json in our execution directory
            with open(os.path.join(rundir, 'testflinger.json'), 'w') as f:
                json.dump(job_data, f)
            # Create json outcome file where phases will store their output
            with open(os.path.join(rundir, 'testflinger-outcome.json'),
                      'w') as f:
                json.dump({}, f)

            for phase in TEST_PHASES:
                # First make sure the job hasn't been cancelled
                if self.check_job_state(job.job_id) == 'cancelled':
                    logger.info("Job cancellation was requested, exiting.")
                    break
                # Try to update the job_state on the testflinger server
                try:
                    self.client.post_result(job.job_id, {'job_state': phase})
                except TFServerError:
                    pass
                proc = multiprocessing.Process(target=job.run_test_phase,
                                               args=(phase, rundir,))
                proc.start()
                while proc.is_alive():
                    proc.join(10)
                    if (self.check_job_state(job.job_id) == 'cancelled' and
                            phase != 'provision'):
                        logger.info("Job cancellation was requested, exiting.")
                        proc.terminate()
                exitcode = proc.exitcode

                # exit code 46 is our indication that recovery failed!
                # In this case, we need to mark the device offline
                if exitcode == 46:
                    self.mark_device_offline()
                    self.client.repost_job(job_data)
                    shutil.rmtree(rundir)
                    # Return NOW so we don't keep trying to process jobs
                    return
                if phase != 'test' and exitcode:
                    logger.debug('Phase %s failed, aborting job' % phase)
                    break

            # Always run the cleanup, even if the job was cancelled
            proc = multiprocessing.Process(target=job.run_test_phase,
                                           args=('cleanup', rundir,))
            proc.start()
            proc.join()

            try:
                self.client.transmit_job_outcome(rundir)
            except Exception as e:
                # TFServerError will happen if we get other-than-good status
                # Other errors can happen too for things like connection
                # problems
                logger.exception(e)
                results_basedir = self.client.config.get('results_basedir')
                shutil.move(rundir, results_basedir)

            if self.check_offline():
                # Don't get a new job if we are now marked offline
                break
            job_data = self.client.check_jobs()

    def retry_old_results(self):
        """Retry sending results that we previously failed to send"""

        results_dir = self.client.config.get('results_basedir')
        # List all the directories in 'results_basedir', where we store the
        # results that we couldn't transmit before
        old_results = [os.path.join(results_dir, d)
                       for d in os.listdir(results_dir)
                       if os.path.isdir(os.path.join(results_dir, d))]
        for result in old_results:
            try:
                logger.info('Attempting to send result: %s' % result)
                self.client.transmit_job_outcome(result)
            except TFServerError:
                # Problems still, better luck next time?
                pass
