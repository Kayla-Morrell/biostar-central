import difflib
import logging
import uuid, copy
import os
import subprocess
from mimetypes import guess_type

import hjson
from django.conf import settings
from django.contrib import messages
from django.contrib.messages.storage import fallback
from django.db.models import Q
from django.template import Template, Context
from django.template import loader
from django.test import RequestFactory
from django.utils.safestring import mark_safe
from django.utils.timezone import now

from . import models
from . import util
from .const import *
from .models import Data, Analysis, Job, Project, Access

logger = logging.getLogger("engine")

JOB_COLORS = {Job.SPOOLED: "spooled",
              Job.ERROR: "errored", Job.QUEUED: "queued",
              Job.RUNNING: "running", Job.COMPLETED: "completed"
              }

def get_uuid(limit=32):
    return str(uuid.uuid4())[:limit]


def join(*args):
    return os.path.abspath(os.path.join(*args))


def access_denied_message(user, needed_access):
    """
    Generates the access denied message
    """
    tmpl = loader.get_template('widgets/access_denied_message.html')

    # Get the string format of the access.
    needed_access = dict(Access.ACCESS_CHOICES).get(needed_access)

    context = dict(user=user, needed_access=needed_access)
    return tmpl.render(context=context)


def copy_file(request, fullpath):
    if not os.path.exists(fullpath):
        messages.error(request, "Path does not exist.")
        return []

    if request.user.is_anonymous:
        messages.error(request, "You need to be logged in.")
        return []

    clipboard = request.session.get(settings.CLIPBOARD_NAME, {})

    board_items = clipboard.get(COPIED_FILES, [])
    board_items.append(fullpath)
    # No duplicates in clipboard
    items = list(set(board_items))
    clipboard[COPIED_FILES] = items

    request.session.update({settings.CLIPBOARD_NAME: clipboard})
    return items


def copy_uid(request, uid, board):
    """
    Used to append instance.uid into request.session[board]
    """
    if request.user.is_anonymous:
        messages.error(request, "You need to be logged in.")
        return []

    clipboard = request.session.get(settings.CLIPBOARD_NAME, {})

    board_items = clipboard.get(board, [])
    board_items.append(uid)
    # No duplicates in clipboard
    clipboard[board] = list(set(board_items))

    request.session.update({settings.CLIPBOARD_NAME: clipboard})

    return board_items


def authorize_run(user, recipe):
    """
    Returns runnable.
    """
    # An anonymous user cannot run recipes.
    if user.is_anonymous:
        return False

    # Only users with access can run recipes
    readable = is_readable(user=user, project=recipe.project)

    if not readable:
        return False

    # A trusted user can run recipes that they have access to.
    if user.profile.trusted and recipe.runnable():
        return True

    # A superuser can run all recipes.
    if user.is_superuser:
        return True

    return False


def generate_script(job):
    """
    Generates a script from a job.
    """
    work_dir = job.path
    json_data = hjson.loads(job.json_text)

    # The base url to the site.
    url_base = f'{settings.PROTOCOL}://{settings.SITE_DOMAIN}{settings.HTTP_PORT}'

    # Extra context added to the script.
    runtime = dict(
        media_root=settings.MEDIA_ROOT,
        media_url=settings.MEDIA_URL,
        work_dir=work_dir, local_root=settings.LOCAL_ROOT,
        user_id=job.owner.id, user_email=job.owner.email,
        job_id=job.id, job_name=job.name,
        job_url=f'{url_base}{settings.MEDIA_URL}{job.get_url()}'.rstrip("/"),
        project_id=job.project.id, project_name=job.project.name,
        analyis_name=job.analysis.name,
        analysis_id=job.analysis.id,
        domain=settings.SITE_DOMAIN, protocol=settings.PROTOCOL,
    )

    # Add the runtime context to the data.
    json_data['runtime'] = runtime
    try:
        # Generate the script.
        template = Template(job.template)
    except Exception as exc:
        template = Template(f"Error loading script template : \n{exc}.")

    context = Context(json_data)
    script = template.render(context)

    return json_data, script


def detect_cores(request):
    # Check if the Origin in the request is allowed
    origin = request.headers.get('Origin', '')
    if origin in settings.CORS_ORIGIN_WHITELIST:
        return origin

    return ''


def link_file(source, target_dir):
    base, filename = os.path.split(source)
    target = os.path.join(target_dir, filename)

    # Link the file if it do
    if not os.path.exists(target):
        # Ensure base dir exists in target
        os.makedirs(target_dir, exist_ok=True)
        os.symlink(source, target)

    return target


def add_file(target_dir, source):
    """
    Deposit file stream into a target directory.
    """

    # Link an existing file
    if isinstance(source, str) and os.path.exists(source):
        return link_file(source=source, target_dir=target_dir)

    # Write a stream to a new file
    if hasattr(source, 'read'):
        # Get the absolute path
        dest = os.path.abspath(target_dir)

        # Create the directory
        os.makedirs(dest, exist_ok=True)

        # Get the name
        fname = source.name

        path = os.path.abspath(os.path.join(dest, fname))
        # Write the stream into file.
        util.write_stream(stream=source, dest=path)

        return path

    return


def get_project_list(user, include_public=True, include_deleted=False):
    """
    Return projects visible to a user.
    """
    privacy = None
    if include_public:
        privacy = Project.PUBLIC

    if user.is_anonymous:
        # Unauthenticated users see public projects.
        cond = Q(privacy=Project.PUBLIC)
    else:
        # Authenticated users see public projects and private projects with access rights.
        cond = Q(owner=user, privacy=Project.PRIVATE) | Q(privacy=privacy) | Q(access__user=user,
                                                                               access__access__in=[Access.READ_ACCESS,
                                                                                                   Access.WRITE_ACCESS,
                                                                                                   Access.SHARE_ACCESS])
    # Generate the query.
    if include_deleted:
        query = Project.objects.filter(cond).distinct()
    else:
        query = Project.objects.filter(cond, deleted=False).distinct()

    #query = query.prefetch_related('access_set', 'data_set', 'analysis_set', 'job_set').select_related('owner', 'owner__profile')
    return query


def create_project(user, name, uid=None, summary='', text='', stream=None,
                   privacy=Project.PRIVATE, update=False):
    # Set or create the project uid.
    uid = uid or util.get_uuid(8)

    # Attempts to select the project.
    project = Project.objects.filter(uid=uid)

    # If it is not an update request return the project unchanged.
    if project and not update:
        return project.first()

    if project:
        # Update existing project.
        current = project.first()
        text = text or current.text
        name = name or current.name
        project.update(text=text, name=name)
        project = project.first()
        logger.info(f"Updated project: {project.name} uid: {project.uid}")
    else:
        # Create a new project.
        project = Project.objects.create(
            name=name, uid=uid, text=text, owner=user, privacy=privacy)

        logger.info(f"Created project: {project.name} uid: {project.uid}")

    # Update the image for the project.
    if stream:
        project.image.save(stream.name, stream, save=True)

    return project


def create_analysis(project, json_text, template, uid=None, user=None, summary='',
                    name='', text='', stream=None, security=Analysis.NOT_AUTHORIZED, update=False,
                    root=None):

    owner = user or project.owner

    analysis = Analysis.objects.filter(uid=uid)


    # Only update when there is a flag
    if analysis and update:
        # Update analysis
        current = analysis.first()
        text = text or current.text
        name = name or current.name
        template = template or current.template
        json_text = json_text or current.json_text
        analysis.update(text=text, name=name, template=template, json_text=json_text)
        analysis = analysis.first()
        logger.info(f"Updated analysis: uid={analysis.uid} name={analysis.name}")
    else:
        # Create a new analysis
        uid = None if analysis else uid
        analysis = Analysis.objects.create(project=project, uid=uid, json_text=json_text,
                                           owner=owner, name=name, text=text, security=security,
                                           template=template, root=root)

        analysis.uid = f"recipe-{analysis.id}-{util.get_uuid(3)}" if not uid else uid
        analysis.save()

        # Update the projects lastedit user when a recipe is created
        Project.objects.filter(uid=analysis.project.uid).update(lastedit_user=user,
                                                                lastedit_date=now())

        logger.info(f"Created analysis: uid={analysis.uid} name={analysis.name}")

    if stream:
        analysis.image.save(stream.name, stream, save=True)

    return analysis


def make_job_title(recipe, data):
    """
    Creates informative job title that shows job parameters.
    """
    params = data.values()

    # Extracts the field that gets displayed for a parameter
    def extract(param):
        if not param.get("display"):
            return None
        if param.get("source"):
            return param.get("name")
        if param.get('display') == UPLOAD:
            return os.path.basename(param.get('value')) if param.get('value') else None
        return param.get("value")

    vals = map(extract, params)
    vals = filter(None, vals)
    vals = map(str, vals)
    vals = ", ".join(vals)

    if vals:
        name = f"Results for {recipe.name}: {vals}"
    else:
        name = f"Results for {recipe.name}"

    return name


def validate_recipe_run(user, recipe):
    """
    Validate that a user can run a given recipe.
    """
    if user.is_anonymous:
        msg = "You must be logged in."
        return False, msg

    if not authorize_run(user=user, recipe=recipe):
        msg = "Insufficient permission to execute recipe."
        return False, msg

    if recipe.deleted:
        msg = "Can not run a deleted recipe."
        return False, msg

    # Not trusted users have job limits.
    running_jobs = Job.objects.filter(owner=user, state=Job.RUNNING)
    if not user.profile.trusted and running_jobs.count() >= settings.MAX_RUNNING_JOBS:
        msg = "Exceeded maximum amount of running jobs allowed. Please wait until some finish."
        return False, msg

    return True, ""


def fill_json_data(project, job=None, source_data={}, fill_with={}):
    """
    Produces a filled in JSON data based on user input.
    """

    # Creates a data.id to data mapping.
    store = dict((data.id, data) for data in project.data_set.all())

    # Make a copy of the original json data used to render the form.
    json_data = copy.deepcopy(source_data)

    # Get default dictionary to fill with from json data 'value'
    default = {field: item.get('value', '') for field, item in json_data.items()}
    fill_with = fill_with or default

    # Alter the json data and fill in the extra information.
    for field, item in json_data.items():

        # If the field is a data field then fill in more information.
        if item.get("source") == "PROJECT" and fill_with.get(field, '').isalnum():
            data_id = int(fill_with.get(field))
            data = store.get(data_id)
            # This mutates the `item` dictionary!
            data.fill_dict(item)
            continue

        # The JSON value will be overwritten with the selected field value.
        if field in fill_with:
            item["value"] = fill_with[field]
            # Clean the textbox value
            if item.get('display') == TEXTBOX:
                item["value"] = util.clean_text(fill_with[field])

            if item.get('display') == UPLOAD:
                # Add uploaded file to job directory.
                upload_value = fill_with.get(field)
                if not upload_value:
                    item['value'] = ''
                    continue
                # Link or write the stream located in the fill_with
                path = add_file(target_dir=job.get_data_dir(), source=upload_value)
                item['value'] = path

    return json_data


def create_job(analysis, user=None, json_text='', json_data={}, name=None, state=Job.QUEUED, uid=None, save=True,
               fill_with={}):
    """
    Note: Parameter 'fill_with' needs to be a flat key:value dictionary.
    """
    state = state or Job.QUEUED
    owner = user or analysis.project.owner
    project = analysis.project

    if json_data:
        json_text = hjson.dumps(json_data)
    else:
        json_text = json_text or analysis.json_text

    # Needs the json_data to set the summary.
    json_data = hjson.loads(json_text)

    # Generate a meaningful job title.
    name = make_job_title(recipe=analysis, data=json_data)
    uid = uid or util.get_uuid(8)

    # Create the job instance.
    job = Job(name=name, state=state, json_text=json_text,
              security=Job.AUTHORIZED, project=project, analysis=analysis, owner=owner,
              template=analysis.template, uid=uid)

    # Fill the json data.
    json_data = fill_json_data(job=job, source_data=json_data, project=project, fill_with=fill_with)

    # Generate a meaningful job title.
    name = make_job_title(recipe=analysis, data=json_data)
    # Update the json_text and name
    job.json_text = hjson.dumps(json_data)
    job.name = name

    if save:
        job.save()

        # Update the projects lastedit user when a job is created
        #job_count = project.job_set.filter(deleted=False).count()
        job_count = Job.objects.filter(deleted=False, project=project).count()
        Project.objects.filter(uid=project.uid).update(lastedit_user=owner,
                                                       lastedit_date=now(),
                                                       jobs_count=job_count)
        logger.info(f"Created job id={job.id} name={job.name}")

    return job


def delete_object(obj, request):
    access = is_writable(user=request.user, project=obj.project)

    # Toggle the delete state if the user has write access
    if access:
        obj.deleted = not obj.deleted
        obj.save()
        data_count = Data.objects.filter(deleted=False, project=obj.project).count()
        recipes_count = Analysis.objects.filter(deleted=False, project=obj.project).count()
        job_count = Job.objects.filter(deleted=False, project=obj.project).count()

        Project.objects.filter(uid=obj.project.uid).update(data_count=data_count,
                                                           recipes_count=recipes_count,
                                                           jobs_count=job_count)

    return obj.deleted


def listing(root):
    paths = []

    try:
        paths = os.listdir(root)

        def transform(path):
            path = os.path.join(root, path)
            tstamp = os.stat(path).st_mtime
            size = os.stat(path).st_size
            rel_path = os.path.relpath(path, settings.IMPORT_ROOT_DIR)
            is_dir = os.path.isdir(path)
            basename = os.path.basename(path)
            return rel_path, tstamp, size, is_dir, basename

        paths = map(transform, paths)
        # Sort files by timestamps
        paths = sorted(paths, key=lambda x: x[1], reverse=True)

    except Exception as exc:
        logging.error(exc)

    return paths

def job_color(job):
    try:
        if isinstance(job, Job):
            return JOB_COLORS.get(job.state, "")
    except Exception as exc:
        logger.error(exc)
        return ''

    return

def guess_mimetype(fname):
    "Return mimetype for a known text filename"

    mimetype, encoding = guess_type(fname)

    ext = os.path.splitext(fname)[1].lower()

    # Known text extensions ( .fasta, .fastq, etc.. )
    if ext in KNOWN_TEXT_EXTENSIONS:
        mimetype = 'text/plain'

    return mimetype


def create_path(fname, data):
    """
    Returns a proposed path based on fname to the storage folder of the data.
    Attempts to preserve the extension but also removes all whitespace from the filenames.
    """
    # Select the file name.

    fname = os.path.basename(fname)

    # The data storage directory.
    data_dir = data.get_data_dir()

    # Make the data directory if it does not exist.
    os.makedirs(data_dir, exist_ok=True)

    # Build the file name under the new location.
    path = os.path.abspath(os.path.join(data_dir, fname))

    return path


def link_data(path, data):
    dest = create_path(fname=path, data=data)

    if not os.path.exists(dest):
        os.symlink(path, dest)

    return dest


def is_readable(user, project):
    # Shareable projects can get to see the

    query = Q(access=Access.READ_ACCESS) | Q(access=Access.WRITE_ACCESS) | Q(access=Access.SHARE_ACCESS)

    readable = Access.objects.filter(query, project=project, user=user)

    return readable.exists()


def is_writable(user, project, owner=None):
    """
    Returns True if a user has write access to an instance
    """

    # Anonymous user may not have write access.
    if user.is_anonymous:
        return False

    # Users that may access a project.
    cond1 = user.is_staff or user.is_superuser

    # User has been given write access to the project
    cond2 = models.Access.objects.filter(user=user, project=project,
                                         access=models.Access.WRITE_ACCESS).first()

    # User owns this project.
    owner = owner or project.owner
    cond3 = user == owner

    # One of the conditions has to be true.
    access = cond1 or cond2 or cond3

    return access


def writeable_recipe(user, source, project=None):
    """
    Check if a user can write to a 'source' recipe.
    """
    if user.is_anonymous:
        return False

    if source.is_cloned:
        # Check write access using root recipe information for clones.
        target_owner = source.root.owner
        project = source.root.project

    else:
        target_owner = source.owner
        project = project or source.project

    access = is_writable(user=user, project=project, owner=target_owner)
    return access


def fill_data_by_name(project, json_data):
    """
    Fills json information by name.
    Used when filling in demonstration data and not user selection.
    """

    json_data = copy.deepcopy(json_data)
    # A mapping of data by name

    for field, item in json_data.items():
        # If the field is a data field then fill in more information.
        val = item.get("value", '')
        if item.get("source") == "PROJECT":
            name = item.get("value")

            item['toc'] = "FILE-LIST"
            item['file_list'] = "FILE-LIST"
            item['value'] = name or 'FILENAME'
            item['data_dir'] = "DATA_DIR"
            item['id'] = "DATA_ID"
            item['name'] = "DATA_NAME"
            item['uid'] = "DATA_UID"
            item['project_dir'] = project.get_data_dir()
            item['data_url'] = "/"

            continue

        # Give a placeholder so templates do not have **MISSING**.
        if val is None or len(str(val)) == 0:
            item['value'] = f'{str(field).upper()}'

    return json_data


def create_data(project, user=None, stream=None, path='', name='',
                text='', type='', uid=None, summary=''):
    # We need absolute paths with no trailing slashes.
    path = os.path.abspath(path).rstrip("/")

    # Create the data.
    type = type or "DATA"

    # Set the object unique id.
    uid = uid or util.get_uuid(8)

    # The owner of the data will be the first admin user if not set otherwise.
    owner = user or models.User.objects.filter(is_staff=True).first()

    # Create the data object.
    data = Data.objects.create(name=name, owner=owner, state=Data.PENDING, project=project,
                               type=type, text=text, uid=uid)

    # The source of the data is a stream and will become the path
    # that should be added.
    if stream:
        name = name or stream.name
        fname = '_'.join(name.split())
        path = create_path(data=data, fname=fname)
        util.write_stream(stream=stream, dest=path)
        # Mark incoming file as uploaded
        data.method = Data.UPLOAD

    # The path is a file.
    isfile = os.path.isfile(path)

    # The path is a directory.
    isdir = os.path.isdir(path)

    # The data is a single file on a path.
    if isfile:
        link_data(path=path, data=data)
        logger.info(f"Linked file: {path}")

    # The data is a directory.
    # Link each file of the directory into the storage directory.
    if isdir:
        for p in os.scandir(path):
            link_data(path=p.path, data=data)
            logger.info(f"Linked file: {p}")

    # Invalid paths and empty streams still create the data
    # but set the data state will be set to error.
    missing = not (path or stream)

    # An invalid entry here.
    if path and missing:
        state = Data.ERROR
        logger.error(f"Invalid data path: {path}")
    else:
        state = Data.READY

    # Make the table of content file with files in data_dir.
    data.make_toc()

    # Set updated attributes
    data.state = state
    data.name = name or os.path.basename(path) or 'Data'

    # Trigger another save.
    data.save()

    # Update the projects lastedit user when a data is uploaded
    Project.objects.filter(uid=data.project.uid).update(lastedit_user=user,
                                                        lastedit_date=now())

    # Set log for data creation.
    logger.info(f"Added data type={data.type} name={data.name} pk={data.pk}")

    return data
