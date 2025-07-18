# SimpLui
Fork of the Luigi framework with a different object model

NOTE: This doesn't actually work yet.  It's bound to change quite a bit as time goes on.  I'm building this from the flow development back to the root API level, trying to keep as much of Luigi as I can.  But at this point, even the flow development level is in flux, so there's no guarantees that any of it will work at all.

There are many great things about the Luigi Framework, but IMHO it's not quite as perfect as it could be.  Mainly, having to have each job be an individual class.  There are many make/build written in Python that let you turn any function into a task, always seemed a little to simplistic.  But having each job be it's own class seems more complicated than necessary.  The concept of namespaces to hold Tasks is also a little too implicit, my preference would be for a more explicit definition.
Here's the plan for this framework:

1. Tasks will be set up as instances of a Task class.  This should allow the Task classes to be more like a template, that let's you define a basic job that can be customized at the instance level.  It should also be possible to set up a library of Task classes that can be used and customized for standard task types.
2. There will be a defined Flow object that can be set up to hold the Task instances.  This should make it easier to define things like subflows, daily recurring flows, and so on.  Task instances will have to be explicitly added to a Flow instance.  Still thinking about other functionality that can be added to Flow objects.
3. The scheduler object instance should be connected to the Flow object.  Ideally, it should be possilbe to define and set up the scheduler in Python, so there shouldn't need to be a config file with different syntax.  Having it available in Python should make it possible to set up a basic config and import it to the flow definition.
4. The whole framework should also support a more make-like flow.  Ideally, the ability to stop, start, and rerun jobs using the web interface should be possible.  Making flows more interactive is a major goal.
5. With all the basic building blocks defined as class instances, it should be possible to make the framework very flexible.  It should be able to replace things like CMake at the basic build level, up to Jenkins for CI/CD flows, and different levels in between.  

So those are the goals, we'll see how much of a success it winds up being.  I'm also hoping to be able to add in some of the extra capability from the "law" variant.  But as I said earlier, none of this works yet.
